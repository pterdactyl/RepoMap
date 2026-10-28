from pathlib import Path
from graphviz import Digraph

from .scanner import load_repository_map


def visualize_repository(
    input_file=Path("repository_map.json"),
    output_file=Path("repository_map.html"),
):
    input_file = Path(input_file)
    output_file = Path(output_file)

    results = load_repository_map(input_file)

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

    directories = {}

    for result in results:
        file_path = Path(result["file"])
        directory = file_path.parent.as_posix()

        if directory == ".":
            directory = "Root"

        directories.setdefault(directory, []).append(result)

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