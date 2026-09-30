from __future__ import annotations

import json
from pathlib import Path
import tempfile
from typing import Any

from .backend import CodeGraphBackendError, JoernDockerBackend


OPSS_JSON_MARKER = "OPSS_JSON="

STRUCTURAL_QUERY_SCRIPT = r"""
@main def exec(
  cpgFile: String,
  mode: String,
  symbol: String,
  target: String,
  maxDepth: Int,
  limit: Int,
  includeExternal: Boolean
) = {
  importCpg(cpgFile)
  import scala.collection.mutable

  type Method = io.shiftleft.codepropertygraph.generated.nodes.Method

  def key(m: Method): String =
    s"${m.fullName}|${m.filename}|${m.lineNumber.getOrElse(-1)}"

  def unique(methods: List[Method]): List[Method] =
    methods.groupBy(key).values.map(_.head).toList.sortBy(key)

  def resolve(value: String): (String, List[Method]) = {
    if (value.isEmpty) {
      ("none", List.empty[Method])
    } else {
      val byFullName = unique(cpg.method.fullNameExact(value).l)
      if (byFullName.nonEmpty) ("full_name", byFullName)
      else ("name", unique(cpg.method.nameExact(value).l))
    }
  }

  def visible(m: Method): Boolean =
    includeExternal || (!m.isExternal && !m.name.startsWith("<operator>."))

  def row(m: Method, depth: Int): ujson.Obj =
    ujson.Obj(
      "name" -> m.name,
      "full_name" -> m.fullName,
      "file" -> m.filename,
      "line" -> m.lineNumber.getOrElse(-1),
      "is_external" -> m.isExternal,
      "depth" -> depth
    )

  def neighbors(m: Method, reverse: Boolean): List[Method] = {
    val values =
      if (reverse) m.caller(NoResolve).l
      else m.callee(NoResolve).l
    unique(values).filter(visible)
  }

  def walk(seed: Method, reverse: Boolean, depthLimit: Int): List[(Method, Int)] = {
    val queue = mutable.Queue[(Method, Int)]((seed, 0))
    val seen = mutable.Set[String]()
    val result = mutable.ArrayBuffer[(Method, Int)]()
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

  def findPath(source: Method, destination: Method, depthLimit: Int): List[Method] = {
    val destinationKey = key(destination)
    val queue = mutable.Queue[(Method, List[Method], Int)]((source, List(source), 0))
    val seen = mutable.Set[String]()
    var found = List.empty[Method]
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

  val (resolution, matches) = resolve(symbol)
  val (targetResolution, targetMatches) =
    if (mode == "path") resolve(target) else ("none", List.empty[Method])

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
      resultRows = path.take(limit).zipWithIndex.map { case (method, depth) => row(method, depth) }
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
  println("OPSS_JSON=" + ujson.write(payload))
}
"""


class CodeGraphQueryError(RuntimeError):
    """Raised when a Joern semantic query cannot produce a valid structured result."""


def extract_opss_json(output: str) -> dict[str, Any]:
    for line in reversed(output.splitlines()):
        if not line.startswith(OPSS_JSON_MARKER):
            continue
        raw = line[len(OPSS_JSON_MARKER) :]
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CodeGraphQueryError(f"Invalid OPSS_JSON payload: {exc}") from exc
        if not isinstance(parsed, dict):
            raise CodeGraphQueryError("OPSS_JSON payload must be a JSON object.")
        return parsed
    raise CodeGraphQueryError("Joern query completed without an OPSS_JSON result marker.")


class JoernStructuralQueryEngine:
    def __init__(self, backend: JoernDockerBackend) -> None:
        self.backend = backend

    def run(
        self,
        *,
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

        with tempfile.TemporaryDirectory(prefix="opss-codegraph-query-") as temp_dir:
            script_path = Path(temp_dir) / "query.sc"
            script_path.write_text(STRUCTURAL_QUERY_SCRIPT, encoding="utf-8")
            try:
                process_result = self.backend.query(
                    cpg_path=cpg_path,
                    script_path=script_path,
                    params=[
                        ("cpgFile", "/cpg.bin"),
                        ("mode", mode),
                        ("symbol", symbol),
                        ("target", target),
                        ("maxDepth", str(max_depth)),
                        ("limit", str(limit)),
                        ("includeExternal", "true" if include_external else "false"),
                    ],
                )
            except CodeGraphBackendError:
                raise

        payload = extract_opss_json(process_result.stdout_tail)
        payload["query_duration_seconds"] = round(process_result.duration_seconds, 6)
        return payload
