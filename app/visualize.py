import json
from pathlib import Path
from graphviz import Digraph


def visualize_repository(
    input_file=Path("repository_map.json"),
    output_file=Path("repository_map.html"),
):
    input_file = Path(input_file)
    output_file = Path(output_file)

    with input_file.open("r") as file:
        results = json.load(file)

    graph = Digraph("RepoMap", engine="dot", format="svg")
    graph.attr(
        bgcolor="#f8fafc",
        splines="true",
        overlap="false",
        sep="+18",
        esep="+8",
        pack="true",
        packmode="cluster",
        pad="0.4",
    )
    graph.attr(
        "node",
        shape="plain",
        fontname="Helvetica",
    )
    graph.attr(
        "edge",
        color="#64748b",
        arrowsize="0.7",
    )

    file_ids = {
        result["file"]: result["id"]
        for result in results
    }

    # Group files into directory clusters.
    directories = {}

    for result in results:
        file_path = Path(result["file"])
        directory = file_path.parent.as_posix()

        if directory == ".":
            directory = "Root"

        directories.setdefault(directory, []).append(result)

    # Create one visual group per directory.
    for directory, files in directories.items():
        cluster_id = "cluster_" + str(
            abs(hash(directory))
        )

        with graph.subgraph(name=cluster_id) as cluster:
            cluster.attr(
                label=directory,
                style="rounded,filled",
                color="#cbd5e1",
                fillcolor="#eef2ff",
                fontname="Helvetica",
                fontsize="14",
                labelloc="t",
            )

            for result in files:
                file_name = Path(result["file"]).name
                rows = [
                    f'<TR><TD BGCOLOR="#dbeafe">'
                    f'<B>{file_name}</B></TD></TR>'
                ]

                for cls in result.get("classes", []):
                    rows.append(
                        f'<TR><TD ALIGN="LEFT">'
                        f'Class: {cls["name"]}</TD></TR>'
                    )

                    for method in cls.get("methods", []):
                        rows.append(
                            f'<TR><TD ALIGN="LEFT">'
                            f'  {method["name"]}()'
                            f'</TD></TR>'
                        )

                for function in result.get("functions", []):
                    rows.append(
                        f'<TR><TD ALIGN="LEFT">'
                        f'{function["name"]}()'
                        f'</TD></TR>'
                    )

                table = (
                    '<TABLE BORDER="1" CELLBORDER="0" '
                    'CELLSPACING="0" CELLPADDING="6" '
                    'COLOR="#94a3b8" BGCOLOR="white">'
                    + "".join(rows)
                    + "</TABLE>"
                )

                cluster.node(
                    result["id"],
                    label=f"<{table}>",
                )

    # Connect files that import each other.
    # for result in results:
    #     source_id = result["id"]

    #     for dependency in result.get("dependencies", []):
    #         target_id = file_ids.get(dependency)

    #         if target_id:
    #             graph.edge(
    #                 source_id,
    #                 target_id,
    #                 # label="imports",
    #                 color="#2563eb",
    #                 penwidth="2",
    #                 fontcolor="#2563eb",
    #             )

    # Graphviz produces a standalone SVG diagram.
    output_file.parent.mkdir(parents=True, exist_ok=True)
    svg_path = output_file.with_suffix(".svg")
    graph.render(
        filename=svg_path.stem,
        directory=str(svg_path.parent),
        cleanup=True,
    )

    print(f"Visualization saved to {svg_path.resolve()}")


if __name__ == "__main__":
    visualize_repository()