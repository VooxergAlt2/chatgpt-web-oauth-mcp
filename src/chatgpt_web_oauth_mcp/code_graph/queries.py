from __future__ import annotations

import base64
import json
from pathlib import Path
import re
from typing import Any

from .server_runtime import JoernQueryServerRuntime


OPSS_B64_MARKER = "OPSS_B64="
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_OPSS_B64_RE = re.compile(r"OPSS_B64=([A-Za-z0-9+/=]+)")


STRUCTURAL_QUERY_BLOCK = r"""{
  import io.shiftleft.codepropertygraph.generated.nodes.Method
  import java.nio.charset.StandardCharsets
  import java.util.Base64
  import scala.collection.mutable

  type MethodNode = io.shiftleft.codepropertygraph.generated.nodes.Method

  def decode(value: String): String =
    new String(Base64.getDecoder.decode(value), StandardCharsets.UTF_8)

  def key(m: MethodNode): String =
    s"${m.fullName}|${m.filename}|${m.lineNumber.getOrElse(-1)}"

  def unique(methods: List[MethodNode]): List[MethodNode] =
    methods.groupBy(key).values.map(_.head).toList.sortBy(key)

  def resolve(value: String): (String, List[MethodNode]) = {
    if (value.isEmpty) {
      ("none", List.empty[MethodNode])
    } else {
      val byFullName = unique(cpg.method.fullNameExact(value).l)
      if (byFullName.nonEmpty) ("full_name", byFullName)
      else ("name", unique(cpg.method.nameExact(value).l))
    }
  }

  def visible(m: MethodNode): Boolean =
    includeExternal || (!m.isExternal && !m.name.startsWith("<operator>."))

  def row(m: MethodNode, depth: Int): ujson.Obj =
    ujson.Obj(
      "name" -> m.name,
      "full_name" -> m.fullName,
      "file" -> m.filename,
      "line" -> m.lineNumber.getOrElse(-1),
      "is_external" -> m.isExternal,
      "depth" -> depth
    )

  def neighbors(m: MethodNode, reverse: Boolean): List[MethodNode] = {
    val values =
      if (reverse) m.caller(NoResolve).l
      else m.callee(NoResolve).l
    unique(values).filter(visible)
  }

  def walk(seed: MethodNode, reverse: Boolean, depthLimit: Int): List[(MethodNode, Int)] = {
    val queue = mutable.Queue[(MethodNode, Int)]((seed, 0))
    val seen = mutable.Set[String]()
    val result = mutable.ArrayBuffer[(MethodNode, Int)]()
    while (queue.nonEmpty) {
      val (method, depth) = queue.dequeue()
      val methodKey = key(method)
      if (!seen.contains(methodKey)) {
        seen += methodKey
        result += ((method, depth))
        if (depth < depthLimit) {
          neighbors(method, reverse).foreach(next => queue.enqueue((next, depth + 1)))
        }
      }
    }
    result.toList
  }

  def findPath(
    source: MethodNode,
    destination: MethodNode,
    depthLimit: Int
  ): List[MethodNode] = {
    val destinationKey = key(destination)
    val queue =
      mutable.Queue[(MethodNode, List[MethodNode], Int)]((source, List(source), 0))
    val seen = mutable.Set[String]()
    var found = List.empty[MethodNode]
    while (queue.nonEmpty && found.isEmpty) {
      val (method, path, depth) = queue.dequeue()
      val methodKey = key(method)
      if (!seen.contains(methodKey)) {
        seen += methodKey
        if (methodKey == destinationKey) {
          found = path
        } else if (depth < depthLimit) {
          neighbors(method, reverse = false).foreach { next =>
            queue.enqueue((next, path :+ next, depth + 1))
          }
        }
      }
    }
    found
  }

  val mode = decode("__MODE_B64__")
  val symbol = decode("__SYMBOL_B64__")
  val target = decode("__TARGET_B64__")
  val maxDepth = __MAX_DEPTH__
  val limit = __LIMIT__
  val includeExternal = __INCLUDE_EXTERNAL__

  val (resolution, matches) = resolve(symbol)
  val (targetResolution, targetMatches) =
    if (mode == "path") resolve(target) else ("none", List.empty[MethodNode])

  val ambiguous = matches.size > 1
  val targetAmbiguous = targetMatches.size > 1
  val notFound = matches.isEmpty
  val targetNotFound = mode == "path" && targetMatches.isEmpty
  val filterPolicy =
    if (includeExternal) "all_methods"
    else "internal_non_operator_methods_only"

  var resultRows = List.empty[ujson.Value]
  var totalResults = 0

  if (!ambiguous && !notFound) {
    val seed = matches.head
    if (mode == "callers" || mode == "callees") {
      val reverse = mode == "callers"
      val methods = neighbors(seed, reverse)
      totalResults = methods.size
      resultRows = methods.take(limit).map(method => row(method, 1))
    } else if (mode == "impact") {
      val methods = walk(seed, reverse = true, maxDepth)
      totalResults = methods.size
      resultRows = methods.take(limit).map { case (method, depth) => row(method, depth) }
    } else if (
      mode == "path" &&
      !targetAmbiguous &&
      !targetNotFound
    ) {
      val path = findPath(seed, targetMatches.head, maxDepth)
      totalResults = path.size
      resultRows = path.take(limit).zipWithIndex.map { case (method, depth) =>
        row(method, depth)
      }
    }
  }

  val payload = ujson.Obj(
    "mode" -> mode,
    "symbol" -> symbol,
    "target" -> target,
    "resolution" -> resolution,
    "target_resolution" -> targetResolution,
    "matches" -> matches.take(limit).map(method => row(method, 0)),
    "target_matches" -> targetMatches.take(limit).map(method => row(method, 0)),
    "total_matches" -> matches.size,
    "total_target_matches" -> targetMatches.size,
    "ambiguous" -> ambiguous,
    "target_ambiguous" -> targetAmbiguous,
    "not_found" -> notFound,
    "target_not_found" -> targetNotFound,
    "include_external" -> includeExternal,
    "filter_policy" -> filterPolicy,
    "max_depth" -> maxDepth,
    "limit" -> limit,
    "total_results" -> totalResults,
    "query_truncated" -> (
      totalResults > limit || matches.size > limit || targetMatches.size > limit
    ),
    "results" -> resultRows
  )

  OPSS_B64_MARKER + Base64.getEncoder.encodeToString(
    ujson.write(payload).getBytes(StandardCharsets.UTF_8)
  )
}"""


class CodeGraphQueryError(RuntimeError):
    """Raised when a Joern semantic query cannot produce a valid structured result."""


def _encode_query_string(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def build_structural_query(
    *,
    mode: str,
    symbol: str,
    target: str,
    max_depth: int,
    limit: int,
    include_external: bool,
) -> str:
    query = STRUCTURAL_QUERY_BLOCK
    replacements = {
        "__MODE_B64__": _encode_query_string(mode),
        "__SYMBOL_B64__": _encode_query_string(symbol),
        "__TARGET_B64__": _encode_query_string(target),
        "__MAX_DEPTH__": str(max_depth),
        "__LIMIT__": str(limit),
        "__INCLUDE_EXTERNAL__": "true" if include_external else "false",
        "OPSS_B64_MARKER": f'"{OPSS_B64_MARKER}"',
    }
    for marker, replacement in replacements.items():
        query = query.replace(marker, replacement)
    return query


def extract_opss_b64(output: str) -> dict[str, Any]:
    clean = _ANSI_RE.sub("", output)
    if OPSS_B64_MARKER not in clean:
        raise CodeGraphQueryError("Joern REST output is missing an OPSS_B64 result marker.")
    matches = _OPSS_B64_RE.findall(clean)
    if not matches:
        raise CodeGraphQueryError("Invalid OPSS_B64 payload: marker is not valid Base64.")
    encoded = matches[-1]
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise CodeGraphQueryError(f"Invalid OPSS_B64 payload: {exc}") from exc
    try:
        parsed = json.loads(decoded)
    except json.JSONDecodeError as exc:
        raise CodeGraphQueryError(f"Invalid decoded Code Graph JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise CodeGraphQueryError("Decoded Code Graph payload must be a JSON object.")
    return parsed


class JoernStructuralQueryEngine:
    def __init__(self, runtime: JoernQueryServerRuntime) -> None:
        self.runtime = runtime

    def run(
        self,
        *,
        graph_id: str,
        cpg_path: Path,
        mode: str,
        symbol: str,
        target: str = "",
        max_depth: int = 6,
        limit: int = 100,
        include_external: bool = False,
    ) -> dict[str, Any]:
        if mode not in {"callers", "callees", "impact", "path"}:
            raise ValueError(f"Unsupported structural query mode: {mode}")
        if not symbol.strip():
            raise ValueError("symbol must be a non-empty string.")
        if mode == "path" and not target.strip():
            raise ValueError("target must be a non-empty string for path queries.")
        if max_depth < 1 or max_depth > 20:
            raise ValueError("max_depth must be between 1 and 20.")
        if limit < 1 or limit > 200:
            raise ValueError("limit must be between 1 and 200.")

        query = build_structural_query(
            mode=mode,
            symbol=symbol,
            target=target,
            max_depth=max_depth,
            limit=limit,
            include_external=include_external,
        )
        process_result = self.runtime.query(
            graph_id=graph_id,
            cpg_path=cpg_path,
            query=query,
        )
        payload = extract_opss_b64(process_result.stdout)
        payload["query_duration_seconds"] = round(process_result.duration_seconds, 6)
        payload["query_runtime"] = "joern-rest-server"
        payload["query_cold_start"] = process_result.cold_start
        return payload
