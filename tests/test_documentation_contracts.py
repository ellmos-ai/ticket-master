"""Read-only contracts for Markdown structure and source-bound workflow labels."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def mermaid_blocks(text):
    blocks = []
    opened = None
    body = []
    for line in text.splitlines():
        fence = re.fullmatch(r" {0,3}(`{3,}|~{3,})([^`]*)", line)
        if opened is None:
            if fence:
                opened = (fence[1][0], len(fence[1]), fence[2].strip())
                body = []
        elif (fence and fence[1][0] == opened[0]
              and len(fence[1]) >= opened[1] and not fence[2].strip()):
            if opened[2] == "mermaid":
                blocks.append("\n".join(body))
            opened = None
        else:
            body.append(line)
    assert opened is None, "Unclosed Markdown code fence"
    return blocks


def sequence(text):
    blocks = mermaid_blocks(text)
    sequences = [b for b in blocks if b.lstrip().startswith("sequenceDiagram")]
    assert len(sequences) == 1, "Expected one independently fenced sequence diagram"
    return sequences[0]


def test_bilingual_mermaid_diagrams_are_separate_complete_blocks():
    for name in ("README.md", "README_de.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        blocks = mermaid_blocks(text)
        assert any(b.lstrip().startswith("graph TD") for b in blocks), name
        assert all(not ("graph TD" in b and "sequenceDiagram" in b) for b in blocks), name
        assert not re.search(r"^`(?:mermaid)?$", text, re.M), name
        assert sequence(text).splitlines()[1].strip() == "autonumber", name


def test_sbom_index_date_matches_the_referenced_artifact():
    index = (ROOT / "llms.txt").read_text(encoding="utf-8")
    reference = re.search(
        r"Level 1 SBOM text companion: `([^`]+)` \(Stand (\d{4}-\d{2}-\d{2})\)", index
    )
    assert reference, "Missing source-bound SBOM date"
    artifact = (ROOT / reference[1]).read_text(encoding="utf-8")
    audited = re.search(r"^Audited: (\d{4}-\d{2}-\d{2})\b", artifact, re.M)
    assert audited and reference[2] == audited[1], "Index must not invent a newer audit"


def test_sequence_gates_follow_the_canonical_prompt_and_result_order():
    prompt = (ROOT / "prompts/TICKET-MASTER.de.md").read_text(encoding="utf-8")
    assert re.search(r"\*\*GATE 1:\*\* Korrekte Projektzuordnung", prompt)
    assert "**GATE 4 — Erfolgsprüfung:**" in prompt
    for name in ("README.md", "README_de.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        # Inspect semantics independently of the separate Markdown-fence contract.
        diagram = text[text.index("sequenceDiagram"):].split("```", 1)[0]
        participants = dict(re.findall(r"^\s*participant (\w+) as (.+)$", diagram, re.M))
        gate1 = next(key for key, label in participants.items() if "GATE 1" in label)
        result_gates = [key for key, label in participants.items() if "GATE 4" in label]
        assert len(result_gates) == 1, "GATE 4 must be an explicit result-verification stage"
        gate4 = result_gates[0]
        assert "Writer" not in participants[gate1] and "Intake" not in participants[gate1]
        edges = re.findall(r"^\s*(\w+)(?:->>|-->>)(\w+): (.+)$", diagram, re.M)
        dispatch = next(i for i, (a, b, _) in enumerate(edges) if a == "P0" and b == "Sub")
        verify = next(i for i, (a, b, _) in enumerate(edges) if a == "P0" and b == gate4)
        solved = next(i for i, (_, b, label) in enumerate(edges)
                      if b == "FS" and "tickets/SOLVED/" in label)
        assert dispatch < verify < solved, "Result verification must follow delegation"
        assert all("GATE 4" not in label for a, b, label in edges if a == "P0" and b == "Sub")
        assert "(prompts/TICKET-MASTER.de.md)" in text
