"""asyncpg runs one statement per call, so multi-statement SQL scripts are split here."""

from alembic import op


def split_sql(script: str) -> list[str]:
    """Split on top-level semicolons; semicolons inside $$...$$ bodies or quotes are kept."""
    stmts, buf, i, in_dollar, in_quote = [], [], 0, False, False
    while i < len(script):
        if not in_quote and script.startswith("$$", i):
            in_dollar = not in_dollar
            buf.append("$$")
            i += 2
            continue
        ch = script[i]
        if ch == "'" and not in_dollar:
            in_quote = not in_quote
        if ch == ";" and not in_dollar and not in_quote:
            stmts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
        i += 1
    stmts.append("".join(buf).strip())
    return [
        s
        for s in stmts
        if s and not all(line.strip().startswith("--") for line in s.splitlines() if line.strip())
    ]


def exec_script(script: str) -> None:
    for stmt in split_sql(script):
        op.execute(stmt)
