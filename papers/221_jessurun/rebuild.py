from pathlib import Path
import shutil
import sys, subprocess, os
import argparse
import re


def _print_and_run(*cmd, **kwargs):
    print(*cmd)
    return subprocess.run(cmd, **kwargs)


def _find_root_path():
    cur_rootpath = paper_dir
    while not cur_rootpath.joinpath(".git").exists():
        cur_rootpath = cur_rootpath.parent

    if not cur_rootpath.joinpath("publisher").exists():
        print(f"invalid root found: {cur_rootpath}")
        sys.exit(1)
    else:
        print(f"Found rootpath: {cur_rootpath}")
    return cur_rootpath


def _ensure_overleaf_remote_exists():
    existing_remotes = subprocess.run(
        ["git", "remote"], capture_output=True, text=True, cwd=rootpath
    ).stdout.split()
    if "overleaf" in existing_remotes:
        # Nothing more to do
        return
    else:
        repo_name = os.environ.get("OVERLEAF_CURRENT_REPO", None)
        if not repo_name:
            raise ValueError("Must fetch from overleaf, but remote was not set!")
        print("Overleaf remote didn't already exist, adding now")
        _print_and_run(f"git remote add overleaf {repo_name}")


def rm_old_outputs():
    if (outpath := rootpath / "output" / paper_id).exists():
        print("Removing existing output directory")
        shutil.rmtree(outpath)


def sync_overleaf():
    _ensure_overleaf_remote_exists()

    output = _print_and_run(
        "git",
        "fetch",
        "overleaf",
        cwd=rootpath,
        check=True,
        capture_output=True,
        text=True,
    )
    # if output.stdout == "":
    # print("Overleaf remote is up to date, no need to fetch figures and sections")
    # return

    worktree_dir = os.path.relpath(paper_dir, rootpath)
    for pull_path in "figures", "sections", "references.bib", "main.tex":
        # "checkout" ignores local changes, and this must be a mirror
        # so discard these changes and ensure exactly remote files
        # exist
        dest_path = os.path.join(paper_dir, pull_path)
        if os.path.isdir(dest_path):
            shutil.rmtree(dest_path)
        elif os.path.isfile(dest_path):
            os.remove(dest_path)
        _print_and_run(
            "git",
            f"--work-tree={worktree_dir}",
            "checkout",
            f"overleaf/master",
            "--",
            pull_path,
            cwd=rootpath,
            check=True,
        )


def create_rst_sections():
    """
    Converts sections/*.tex to sections/*.rst by changing tex conventions
    to rst conventions.
    """

    # Finds a suitable regex for most latex commands (i.e. emph, ref, etc.)
    def command_regex(cmd, num_args=1):
        return r"\\" + cmd + r"\{(.*?)\}" * num_args

    def env_regex(env, begin=True, end=True):
        assert begin or end, "Must have at least one of begin or end"
        if begin and not end:
            cur_regex = "begin"
        elif end and not begin:
            cur_regex = "end"
        else:
            cur_regex = "(begin|end)"

        return rf"\\{cur_regex}\{{" + env + r"\}"

    def _sanitized_label(text):
        return text.replace(":", "").replace("_", "")

    def section_replace(match):
        groups = match.groups()
        section_name = groups[1]
        if groups[0]:
            replace_char = "-"
        else:
            replace_char = "="
        return f"{section_name}\n{replace_char * len(section_name)}"

    def ref_replace(match):
        groups = match.groups()
        replace_name = _sanitized_label(groups[1])
        if groups[0]:
            # \autoref means "Figure" should be added
            return f"Figure :ref:`{replace_name}`"
        else:
            return f":ref:`{replace_name}`"

    footnotes = []

    def footnote_replace(match):
        footnotes.append(f".. [#] {match.group(1)}")
        return f" [#]_"

    def label_replace(match):
        replace_name = _sanitized_label(match.group(2))
        return f".. _{replace_name}:\n\n{match.group(1)}"

    replace_spec = {
        command_regex("cite"): r":cite:`\1`",
        # Make sure the label can go above the section by capturing the previous line
        r"(.*)\\label\{(.*?)\}": label_replace,
        # Section/subsection
        r"\\(sub)?section\{(.*?)\}": section_replace,
        # ref/autoref
        r"\\(auto)?ref\{(.*?)\}": ref_replace,
        command_regex("emph"): r"*\1*",
        command_regex("texttt"): r"``\1``",
        # I use a separate makefigs.tex file with figure commands, this
        # allows figures to be inserted with "\make...Fig"
        r"(\\make.*Fig)": r".. raw:: latex\n\n    \1",
        # RST can handle quotes just fine
        r'``(.*?)"': r'"\1"',
        # Tilde nbsp doesn't work in RST
        r"~": r" ",
        # Comments start with "%" in latex
        r"(^%.*)": r"\n..\n    \1\n",
        command_regex("href", num_args=2): r"`\2 <\1>`_",
        command_regex("url"): r"`\1 <\1>`_",
        command_regex("footnote"): footnote_replace,
        # Itemize is the only current occurrence of environments
        env_regex("itemize") + "\n": "",
        r"\\item": "*",
        env_regex("lstlisting", begin=True, end=False): "```python",
        env_regex("lstlisting", begin=False, end=True): "```",
    }

    rst_dir = paper_dir / "sections_rst"
    for tex_file in paper_dir.glob("sections/*.tex"):
        footnotes.clear()
        file_text = tex_file.read_text()
        for pattern, replacement in replace_spec.items():
            file_text = re.sub(pattern, replacement, file_text, flags=re.MULTILINE)
        file_text += "\n\n" + "\n".join(footnotes)
        rst_file = rst_dir / tex_file.name.replace(".tex", ".rst")
        rst_file.write_text(file_text)

    # Handle figures file with disallowed reference characters
    def makefig_replace(match):
        return rf"\label{{{_sanitized_label(match.group(1))}}}"

    makefigs_file = paper_dir / "figures/makefigs.tex"
    sanitized_text = re.sub(
        r"\\label\{(.*?)\}", makefig_replace, makefigs_file.read_text()
    )
    makefigs_file.with_name("makefigssanitized.tex").write_text(sanitized_text)


def build_paper():
    builder_script = rootpath / "publisher/build_paper.py"
    paper_relpath = f"papers/{paper_id}"
    subprocess.run(
        [sys.executable, builder_script, paper_relpath], shell=True, cwd=rootpath
    )
    # For some reason, output files are added to git, fix with "git reset"
    subprocess.run(["git", "reset"], capture_output=True, cwd=rootpath)


def create_argparser():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sync-overleaf",
        action="store_true",
        default=False,
        help="Check for overleaf upstream and fetch changes",
    )
    return parser


paper_id = "221_jessurun"
paper_dir = Path(__file__).resolve().parent
rootpath = _find_root_path()

if __name__ == "__main__":
    parser = create_argparser()
    if parser.parse_args().sync_overleaf:
        sync_overleaf()
    create_rst_sections()
    rm_old_outputs()
    build_paper()
