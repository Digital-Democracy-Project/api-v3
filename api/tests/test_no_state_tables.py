"""OPEN-558 (PLAN-state-specific-handling §6, epic OPEN-536): no new per-state rule in api-v3's code.

The same guard as ddp-broker-py's `test_no_state_tables.py` (BROKER-368) and ddp-sync's SYNC-157 test: this file
reads every module under api/ (never imports it) and fails when

  * a module- or class-level dict, set, frozenset, list or tuple written out in the source is keyed by states: two or
    more state codes ("pa", "NE", ...), or one full state name ("Alabama") or OCD jurisdiction/division id; or
  * any code compares a value against a state literal: `x == "pa"`, `x != "Alabama"`, `x in ("pa", "ne")`,
    `"ocd-jurisdiction/country:us/state:pa/government" == j`.

What a state has belongs in ddp-open-states `jurisdictions.yaml` (read through /ddp/enrollment); how a state's data
is shaped belongs in the scraper or in openstates-core. The code that does this today (plan Appendix A.4) is listed
below with its reason, by file and symbol, never by state: an entry cannot hold a state back or let one through
(Ramon's one-list rule, BROKER-481), and adding one is a decision someone writes down. Each entry is removed when
phase 1 retires that code; the last test fails while an entry outlives its code. An entry covers its whole symbol:
a new state added inside an allowed table or function is not caught, so keep entries few and retire them.

Of the plan's A.4 rows only one is such a table today (Alabama's date-first ordering); the rest are rules keyed by
the shape of a note, a URL or a document type, with no state literal in the code, so this guard cannot see them and
needs no entry for them; the plan tracks them. A comparison against a two-letter word that is not a state
(`order == "id"`) is caught too: give it an entry that says so.
"""

import ast
import os
import re

API_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

STATE_CODES = set(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA "
    "RI SC SD TN TX UT VT VA WA WV WI WY DC PR US USA".lower().split()
)
STATE_NAMES = {
    name.lower()
    for name in (
        "Alabama,Alaska,Arizona,Arkansas,California,Colorado,Connecticut,Delaware,Florida,Georgia,Hawaii,Idaho,"
        "Illinois,Indiana,Iowa,Kansas,Kentucky,Louisiana,Maine,Maryland,Massachusetts,Michigan,Minnesota,Mississippi,"
        "Missouri,Montana,Nebraska,Nevada,New Hampshire,New Jersey,New Mexico,New York,North Carolina,North Dakota,"
        "Ohio,Oklahoma,Oregon,Pennsylvania,Rhode Island,South Carolina,South Dakota,Tennessee,Texas,Utah,Vermont,"
        "Virginia,Washington,West Virginia,Wisconsin,Wyoming,District of Columbia,Puerto Rico,United States"
    ).split(",")
}
OCD_ID = re.compile(
    r"\Aocd-[a-z]+/country:us(/(state|territory|district):[a-z]{2})?(/|\Z)",
    re.IGNORECASE,
)

# (path from the repo root, table name): why it is allowed. Retire the entry with the code (phase 1).
ALLOWED_TABLES = {
    ("api/version_ordering.py", "_DATE_ORDERED_JURISDICTIONS"): (
        "plan A.4 row 1: the synced copy of openstates-core's version rules (OPEN-569 date-first ordering); goes when "
        "api-v3 imports core's version_ordering or reads a stored stage (phase 1, OPEN-538)"
    ),
}

# (path from the repo root, enclosing function, or "<module>"): why its comparisons against a state literal are
# allowed. Retire the entry with the code (phase 1).
ALLOWED_COMPARISONS = {}


def _state(value):
    """The state a string literal names (its code, full name or OCD id, lower-cased), or None."""
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower()
    if lowered in STATE_NAMES or OCD_ID.match(lowered):
        return lowered
    if lowered in STATE_CODES:
        return "us" if lowered == "usa" else lowered
    return None


def _container_items(node):
    """The keys or items of a literal container (or of set/frozenset/tuple/list(...) of one), else None."""
    if isinstance(node, ast.Dict):
        return node.keys
    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        return node.elts
    if (
        isinstance(node, ast.Call)
        and getattr(node.func, "id", None) in ("set", "frozenset", "tuple", "list")
        and len(node.args) == 1
    ):
        return _container_items(node.args[0])
    return None


def _keyed_by_states(node):
    """True when a literal container's keys or items name two or more state codes, or any full state name or OCD
    id. One lone code is not enough: "id", "in", "or" and "me" are words too (the broker's rule)."""
    states = {
        _state(i.value)
        for i in _container_items(node) or []
        if isinstance(i, ast.Constant)
    } - {None}
    return len(states & STATE_CODES) >= 2 or bool(states - STATE_CODES)


def _class_level(body, prefix=""):
    """(prefix, statement) for every statement that runs at module or class level, including inside if / try / with /
    for blocks and nested classes; a function body is not module level (pm-review)."""
    for statement in body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(statement, ast.ClassDef):
            yield from _class_level(statement.body, prefix + statement.name + ".")
            continue
        yield prefix, statement
        for block in ("body", "orelse", "finalbody"):
            yield from _class_level(getattr(statement, block, []), prefix)
        for handler in getattr(statement, "handlers", []):
            yield from _class_level(handler.body, prefix)


def state_tables(source, path):
    """[(path, name)] for each module- or class-level assignment of a literal container keyed by states."""
    found = []
    for prefix, statement in _class_level(ast.parse(source).body):
        if isinstance(statement, ast.Assign):
            targets, value = statement.targets, statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            targets, value = [statement.target], statement.value
        else:
            continue
        if _keyed_by_states(value):
            found += [
                (path, prefix + (t.id if isinstance(t, ast.Name) else ast.unparse(t)))
                for t in targets
            ]
    return found


def _compared_states(node):
    """The state literals one Compare node compares against (==, !=, is, is not, in, not in)."""
    states = []
    operands = [node.left] + node.comparators
    for op, left, right in zip(node.ops, operands, operands[1:]):
        if isinstance(op, (ast.Eq, ast.NotEq, ast.Is, ast.IsNot)):
            sides = [left, right]
        elif isinstance(op, (ast.In, ast.NotIn)):
            sides = [left, right] + list(
                _container_items(right) or []
            )  # `x in "pa"` too
        else:
            continue
        states += [
            s.value for s in sides if isinstance(s, ast.Constant) and _state(s.value)
        ]
    return states


class _Comparisons(ast.NodeVisitor):
    def __init__(self, path):
        self.path, self.scope, self.found = path, [], []

    def _scoped(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scoped

    def visit_Compare(self, node):
        if _compared_states(node):
            self.found.append(
                (self.path, ".".join(self.scope) or "<module>", node.lineno)
            )
        self.generic_visit(node)


def state_comparisons(source, path):
    """[(path, enclosing function or "<module>", line)] for each comparison against a state literal."""
    visitor = _Comparisons(path)
    visitor.visit(ast.parse(source))
    return visitor.found


def api_modules():
    repo = os.path.dirname(API_ROOT)
    for root, dirs, files in os.walk(API_ROOT):
        dirs[:] = [d for d in dirs if d not in ("tests", "__pycache__")]
        for name in sorted(files):
            if name.endswith(".py"):
                path = os.path.join(root, name)
                with open(path, encoding="utf-8-sig") as handle:
                    yield os.path.relpath(path, repo), handle.read()


def test_no_module_keeps_a_table_keyed_by_states():
    offenders = [
        found
        for path, source in api_modules()
        for found in state_tables(source, path)
        if found not in ALLOWED_TABLES
    ]
    assert offenders == [], (
        "a per-state table in api-v3's code: put the fact in ddp-open-states jurisdictions.yaml (served by "
        "/ddp/enrollment) or the data itself, not in a table here (OPEN-558)"
    )


def test_no_code_compares_against_a_state():
    offenders = [
        found
        for path, source in api_modules()
        for found in state_comparisons(source, path)
        if found[:2] not in ALLOWED_COMPARISONS
    ]
    assert offenders == [], (
        "a comparison against a state in api-v3's code: read what the state has from the manifest, or fix the data "
        "upstream, instead of branching on the state here (OPEN-558)"
    )


def test_the_guard_catches_a_planted_table_and_comparison():
    planted = """
NEWEST_FIRST = frozenset({"us", "nc"})
TEST_PRECINCTS = {"FL": "FLORIDA-TEST", "VA": "VIRGINIA-TEST"}
DATE_FIRST: tuple = ("Alabama",)
FEDERAL = ["ocd-jurisdiction/country:us/government"]
FIELDS = ("id", "name")  # one two-letter word is not a table of states
_EASTERN = "America/New_York"


class Rules:
    XML_STATES = set(["pa", "ne"])

    class Nested:
        LIVE = ["az", "ma"]


try:
    ODT_STATES = ("pa", "ne")
except ImportError:
    ODT_STATES = ()
if True:
    UNTRUSTED = {"ma": 1, "az": 2}


def pick(jurisdiction, name, j_id, sort):
    if jurisdiction == "pa":
        return 1
    if "LA" != jurisdiction or name in ("Nebraska", "x"):
        return 2
    if j_id == "ocd-jurisdiction/country:us/state:az/government":
        return 3
    if jurisdiction not in "PA":
        return 4
    return sort == "date"
"""
    assert [name for _, name in state_tables(planted, "x.py")] == [
        "NEWEST_FIRST",
        "TEST_PRECINCTS",
        "DATE_FIRST",
        "FEDERAL",
        "Rules.XML_STATES",
        "Rules.Nested.LIVE",
        "ODT_STATES",
        "UNTRUSTED",
    ]
    assert [
        (symbol, line) for _, symbol, line in state_comparisons(planted, "x.py")
    ] == [
        ("pick", 26),
        ("pick", 28),  # "LA" != jurisdiction
        ("pick", 28),  # name in ("Nebraska", "x")
        ("pick", 30),
        ("pick", 32),  # jurisdiction not in "PA"
    ]


def test_every_allowed_entry_still_exists():
    """An allowance for code that was removed is dead: drop it, so it cannot cover a new rule in its place."""
    tables, comparisons = set(), set()
    for path, source in api_modules():
        tables |= set(state_tables(source, path))
        comparisons |= {found[:2] for found in state_comparisons(source, path)}
    assert set(ALLOWED_TABLES) - tables == set()
    assert set(ALLOWED_COMPARISONS) - comparisons == set()
