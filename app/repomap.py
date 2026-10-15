import argparse
import tempfile
from pathlib import Path

from git import Repo

from .scanner import (
    analyze_repository,
    assign_ids,
    resolve_dependencies,
    resolve_function_calls,
    export_results,
    print_run_summary,
)
from .visualize import visualize_repository
from .workflow_tracer import export_traces, trace_workflows

def generate_map(repo_url):
    with tempfile.TemporaryDirectory() as temp_dir:
        repo_path = Path(temp_dir) / "repository"

        print("Cloning repository...")
        Repo.clone_from(repo_url, repo_path)

        print("Analyzing repository...")
        results = analyze_repository(repo_path)
        results = assign_ids(results, repo_path)
        results = resolve_dependencies(results, repo_path)
        results = resolve_function_calls(results)
        print_run_summary(results)

        output_dir = Path.cwd()
        json_path = output_dir / "repository_map.json"
        html_path = output_dir / "repository_map.html"

        export_results(results, json_path)
        visualize_repository(json_path, html_path)

        traces = trace_workflows(results)
        traces_path = output_dir / "workflow_traces.json"
        export_traces(traces, traces_path)

        print(f"JSON map saved to: {json_path}")
        print(f"Interactive map saved to: {html_path}")
        print(f"Workflow traces saved to: {traces_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate an interactive map of a GitHub repository."
    )
    parser.add_argument("repo_url", help="GitHub repository URL")
    args = parser.parse_args()

    generate_map(args.repo_url)