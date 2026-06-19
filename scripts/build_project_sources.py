"""Generate data/project_sources.json from the official eligible allowlist.

Fills only the facts we actually have (symbol, name, BSC contract, CMC id) and
leaves human-curated fields BLANK — we never invent official websites or X
handles. Curate those by hand later from authoritative sources.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO = Path(__file__).resolve().parent.parent
ELIGIBLE = REPO / "src" / "agent" / "data" / "eligible_tokens.json"
OUT = REPO / "src" / "agent" / "data" / "project_sources.json"


def main() -> None:
    elig = json.loads(ELIGIBLE.read_text(encoding="utf-8"))
    rows = []
    for t in elig:
        rows.append({
            "symbol": t.get("symbol", ""),
            "name": t.get("name", ""),
            "bsc_contract": t.get("contract", "") or "",
            "official_website": "",        # curate manually; do not invent
            "official_x_handle": "",       # curate manually; do not invent
            "cmc_slug_or_id": t.get("id", ""),
            "keywords": [],
            "risk_notes": "",
        })
    OUT.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {len(rows)} project sources -> {OUT.relative_to(REPO)}")
    print("Human fields (official_website / official_x_handle / keywords / risk_notes) "
          "are intentionally blank — curate from authoritative sources only.")


if __name__ == "__main__":
    main()
