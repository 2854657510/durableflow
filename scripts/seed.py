"""建表并注册内置工作流。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import SessionLocal, init_db
from app.services.workflows import seed_builtin_workflows


def main() -> None:
    init_db()
    with SessionLocal() as session:
        definitions = seed_builtin_workflows(session)
    print("已注册工作流：")
    for definition in definitions:
        print(f"  id={definition.id:<3} name={definition.name:<18} version={definition.version}")


if __name__ == "__main__":
    main()
