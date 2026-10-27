import argparse
import tempfile
from pathlib import Path

from git import Repo

from .scanner import (
    SCHEMA_VERSION,
    analyze_repository,
    assign_ids,
    detect_entry_points,
    export_results,
    link_type_ids,
    print_run_summary,
    resolve_dependencies,
    resolve_function_calls,
)
from .visualize import visualize_repository
from .workflow_tracer import export_traces, trace_workflows


def _looks_like_git_url(value):
    if "://" in value:
        return True
    if value.startswith("git@"):
        return True
    if "@" in value:
        host_and_path = value.split("@", 1)[1]
        if ":" in host_and_path and not host_and_path.startswith(":"):
            return True
    return False


def resolve_repository(repository):
    """Return a source descriptor for a local directory or a git URL.

    Existing directories are scanned in place and do not need to be git
    checkouts. Git remotes, including GitHub URLs, are cloned. A value that
    is neither an existing directory nor a remote is an error.
    """
    path = Path(repository).expanduser()
    if path.exists():
        if not path.is_dir():
            raise NotADirectoryError(
                f"{repository} is a file. Pass a directory or a git URL."
            )
        return {"kind": "local", "path": str(path.resolve())}

    if _looks_like_git_url(repository):
        return {"kind": "git", "url": repository}

    raise FileNotFoundError(
        f"No directory at {repository}. Pass a local directory or a git URL."
    )


def _emit_map(repo_path, output_dir, source):
    print("Analyzing repository...")
    results = analyze_repository(repo_path)
    results = assign_ids(results, repo_path)
    results = resolve_dependencies(results, repo_path)
    results = resolve_function_calls(results)
    results = link_type_ids(results)
    results = detect_entry_points(results)
    print_run_summary(results)

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "repository_map.json"
    html_path = output_dir / "repository_map.html"

    export_results(results, json_path, source=source)
    visualize_repository(json_path, html_path)

    traces = trace_workflows(results)
    traces_path = output_dir / "workflow_traces.json"
    export_traces(traces, traces_path)

    print(f"JSON map saved to: {json_path}")
    print(f"Interactive map saved to: {html_path}")
    print(f"Workflow traces saved to: {traces_path}")


def generate_map(repository, output_dir=None):
    if output_dir is None:
        output_dir = Path.cwd()

    source = resolve_repository(repository)
    if source["kind"] == "local":
        print(f"Using local repository: {source['path']}")
        _emit_map(Path(source["path"]), output_dir, source)
        return

    with tempfile.TemporaryDirectory() as temp_dir:
        repo_path = Path(temp_dir) / "repository"
        print("Cloning repository...")
        Repo.clone_from(source["url"], repo_path)
        _emit_map(repo_path, output_dir, source)


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Generate an interactive map of a GitHub repository or a local directory."
        ),
        epilog=(
            f"repository_map.json is written as schema version {SCHEMA_VERSION}. "
            "See schema/repository_map.schema.json."
        ),
    )
    parser.add_argument(
        "repository",
        help=(
            "GitHub (or other git) repository URL, or a path to a local directory. "
            "Existing directories are analyzed in place and do not need to be git checkouts."
        ),
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        generate_map(args.repository)
    except (NotADirectoryError, FileNotFoundError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
