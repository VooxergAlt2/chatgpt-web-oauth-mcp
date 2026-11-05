open System
open System.IO
open System.Text.Json
open Microsoft.Build.Construction

let same left right = String.Equals(left, right, StringComparison.OrdinalIgnoreCase)
let encode value = JsonSerializer.Serialize(value)
let unknown reason = encode {| status = "unknown"; reason = reason |}

let resolve (args: string array) =
    let solution = SolutionFile.Parse(args.[1])
    let projectPath = Path.GetFullPath(args.[2])
    let configurations =
        solution.SolutionConfigurations
        |> Seq.filter (fun c -> same c.ConfigurationName args.[3] && same c.PlatformName args.[4])
        |> Seq.toList
    let projects =
        solution.ProjectsInOrder
        |> Seq.filter (fun p -> p.ProjectType <> SolutionProjectType.SolutionFolder &&
                                same (Path.GetFullPath(p.AbsolutePath)) projectPath)
        |> Seq.toList
    match configurations, projects with
    | [dimension], [project] ->
        match project.ProjectConfigurations.TryGetValue(dimension.FullName) with
        | true, mapping when mapping.IncludeInBuild ->
            encode {| status = "mapped"; configuration = mapping.ConfigurationName; platform = mapping.PlatformName |}
        | true, _ -> encode {| status = "build_disabled" |}
        | _ -> unknown "solution_mapping_missing"
    | [_], [] -> encode {| status = "project_not_in_solution" |}
    | [], _ -> unknown "solution_configuration_missing"
    | _ -> unknown "solution_mapping_ambiguous"

try
    printfn "%s" (resolve fsi.CommandLineArgs)
with _ ->
    printfn "%s" (unknown "solution_parse_failed")
