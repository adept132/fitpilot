import ast
import os
import re
from pathlib import Path
from typing import List, Dict, Optional, Tuple

# ============================================================
# CONFIG
# ============================================================

FRONTEND_DIR = r"C:\Users\Admin\fitpilot-mobile"
BACKEND_DIR = r"C:/Users/Admin/PycharmProjects/FitPilotBot/api"
DB_MODELS_FILE = r"C:\Users\Admin\PycharmProjects\FitPilotBot\api\services\models.py"

# Только эти папки фронта будут включены в анализ
FRONTEND_INCLUDED_DIRS = [
    "app",
    "components",
    "features",
    "hooks",
    "services",
    "store",
    "types",
]

OUTPUT_FILE = "architecture_map.txt"

BACKEND_EXTENSIONS = {".py"}
FRONTEND_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx"}


# ============================================================
# HELPERS
# ============================================================

def safe_read_text(file_path: Path) -> str:
    encodings = ["utf-8", "utf-8-sig", "cp1251", "latin-1"]
    for enc in encodings:
        try:
            return file_path.read_text(encoding=enc)
        except Exception:
            continue
    return ""


def indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line if line else prefix for line in text.splitlines())


def tree_prefix(is_last: bool) -> str:
    return "└── " if is_last else "├── "


def child_prefix(is_last: bool) -> str:
    return "    " if is_last else "│   "


# ============================================================
# BACKEND PARSING
# ============================================================

class BackendParser(ast.NodeVisitor):
    def __init__(self) -> None:
        self.functions: List[str] = []
        self.routes: List[str] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.functions.append(f"def {node.name}()")
        self._extract_route(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.functions.append(f"async def {node.name}()")
        self._extract_route(node)
        self.generic_visit(node)

    def _extract_route(self, node: ast.AST) -> None:
        decorators = getattr(node, "decorator_list", [])
        for dec in decorators:
            route_info = self._parse_router_decorator(dec)
            if route_info:
                self.routes.append(route_info)

    def _parse_router_decorator(self, dec: ast.AST) -> Optional[str]:
        """
        Ищет @router.get(...), @router.post(...), @router.delete(...), etc
        """
        if not isinstance(dec, ast.Call):
            return None

        func = dec.func
        if not isinstance(func, ast.Attribute):
            return None

        if not isinstance(func.value, ast.Name):
            return None

        if func.value.id != "router":
            return None

        method = func.attr

        path_arg = None
        if dec.args:
            first_arg = dec.args[0]
            path_arg = self._literal_to_string(first_arg)

        kwargs = {}
        for kw in dec.keywords:
            if kw.arg:
                kwargs[kw.arg] = self._literal_to_string(kw.value)

        attrs = []
        if path_arg is not None:
            attrs.append(f'path={path_arg}')
        for k, v in kwargs.items():
            attrs.append(f"{k}={v}")

        attrs_joined = ", ".join(attrs)
        return f"@router.{method}({attrs_joined})"

    def _literal_to_string(self, node: ast.AST) -> str:
        try:
            return repr(ast.literal_eval(node))
        except Exception:
            try:
                return ast.unparse(node)
            except Exception:
                return "<dynamic>"


def parse_backend_file(file_path: Path) -> Tuple[List[str], List[str]]:
    text = safe_read_text(file_path)
    if not text.strip():
        return [], []

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [], []

    parser = BackendParser()
    parser.visit(tree)
    return parser.functions, parser.routes


# ============================================================
# FRONTEND PARSING
# ============================================================

FRONTEND_TYPE_RE = re.compile(
    r'^\s*export\s+type\s+([A-Za-z_][A-Za-z0-9_]*)|^\s*type\s+([A-Za-z_][A-Za-z0-9_]*)',
    re.MULTILINE
)

FRONTEND_EXPORT_FUNCTION_RE = re.compile(
    r'^\s*export\s+function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(',
    re.MULTILINE
)

FRONTEND_CONST_RE = re.compile(
    r'^\s*(?:export\s+)?const\s+([A-Za-z_][A-Za-z0-9_]*)\b',
    re.MULTILINE
)

FRONTEND_LET_RE = re.compile(
    r'^\s*(?:export\s+)?let\s+([A-Za-z_][A-Za-z0-9_]*)\b',
    re.MULTILINE
)


def parse_frontend_file(file_path: Path) -> Dict[str, List[str]]:
    text = safe_read_text(file_path)
    if not text.strip():
        return {
            "types": [],
            "export_functions": [],
            "consts": [],
            "lets": [],
        }

    types_found = []
    for match in FRONTEND_TYPE_RE.finditer(text):
        name = match.group(1) or match.group(2)
        if name:
            types_found.append(f"type {name}")

    export_functions = [
        f"export function {name}()"
        for name in FRONTEND_EXPORT_FUNCTION_RE.findall(text)
    ]

    consts = [f"const {name}" for name in FRONTEND_CONST_RE.findall(text)]
    lets = [f"let {name}" for name in FRONTEND_LET_RE.findall(text)]

    # Уберём дубли, сохраняя порядок
    def unique_keep_order(items: List[str]) -> List[str]:
        seen = set()
        result = []
        for item in items:
            if item not in seen:
                seen.add(item)
                result.append(item)
        return result

    return {
        "types": unique_keep_order(types_found),
        "export_functions": unique_keep_order(export_functions),
        "consts": unique_keep_order(consts),
        "lets": unique_keep_order(lets),
    }


# ============================================================
# DB MODELS PARSING
# ============================================================

class SqlAlchemyModelParser(ast.NodeVisitor):
    def __init__(self) -> None:
        self.models: List[Dict] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        model_name = node.name
        tablename = None
        columns = []
        relationships = []
        table_args = []

        for stmt in node.body:
            # __tablename__ = "app_users"
            if isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Name) and target.id == "__tablename__":
                        tablename = self._literal_to_string(stmt.value)

                    elif isinstance(target, ast.Name) and target.id == "__table_args__":
                        table_args = self._parse_table_args(stmt.value)

            # SQLAlchemy 2.0 style:
            # id: Mapped[int] = mapped_column(...)
            # weeks: Mapped[list["PeriodizationWeek"]] = relationship(...)
            if isinstance(stmt, ast.AnnAssign):
                if not isinstance(stmt.target, ast.Name):
                    continue

                field_name = stmt.target.id
                field_type = self._annotation_to_string(stmt.annotation)
                value = stmt.value

                if isinstance(value, ast.Call):
                    call_name = self._get_call_name(value.func)

                    if call_name.endswith("mapped_column") or call_name.endswith("Column"):
                        columns.append(self._parse_column(field_name, field_type, value))

                    elif call_name.endswith("relationship"):
                        relationships.append(self._parse_relationship(field_name, field_type, value))

            # Старый стиль тоже оставим для совместимости:
            # id = Column(...)
            # user = relationship(...)
            elif isinstance(stmt, ast.Assign):
                if len(stmt.targets) != 1 or not isinstance(stmt.targets[0], ast.Name):
                    continue

                field_name = stmt.targets[0].id
                value = stmt.value

                if isinstance(value, ast.Call):
                    call_name = self._get_call_name(value.func)

                    if call_name.endswith("mapped_column") or call_name.endswith("Column"):
                        columns.append(self._parse_column(field_name, None, value))

                    elif call_name.endswith("relationship"):
                        relationships.append(self._parse_relationship(field_name, None, value))

        if tablename or columns or relationships or table_args:
            self.models.append({
                "class_name": model_name,
                "table_name": tablename,
                "columns": columns,
                "relationships": relationships,
                "table_args": table_args,
            })

        self.generic_visit(node)

    def _parse_column(self, field_name: str, field_type: Optional[str], call: ast.Call) -> str:
        args_repr = [self._node_to_string(arg) for arg in call.args]

        kwargs_repr = []
        for kw in call.keywords:
            if kw.arg:
                kwargs_repr.append(f"{kw.arg}={self._node_to_string(kw.value)}")

        joined = ", ".join(args_repr + kwargs_repr)
        if field_type:
            return f"{field_name}: {field_type} = {self._get_call_name(call.func)}({joined})"
        return f"{field_name} = {self._get_call_name(call.func)}({joined})"

    def _parse_relationship(self, field_name: str, field_type: Optional[str], call: ast.Call) -> str:
        args_repr = [self._node_to_string(arg) for arg in call.args]

        kwargs_repr = []
        for kw in call.keywords:
            if kw.arg:
                kwargs_repr.append(f"{kw.arg}={self._node_to_string(kw.value)}")

        joined = ", ".join(args_repr + kwargs_repr)
        if field_type:
            return f"{field_name}: {field_type} = relationship({joined})"
        return f"{field_name} = relationship({joined})"

    def _parse_table_args(self, node: ast.AST) -> List[str]:
        if isinstance(node, ast.Tuple):
            return [self._node_to_string(elt) for elt in node.elts]
        return [self._node_to_string(node)]

    def _annotation_to_string(self, node: ast.AST) -> str:
        return self._node_to_string(node)

    def _get_call_name(self, node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            base = self._get_call_name(node.value)
            return f"{base}.{node.attr}" if base else node.attr
        return ""

    def _literal_to_string(self, node: ast.AST) -> str:
        try:
            return repr(ast.literal_eval(node))
        except Exception:
            return self._node_to_string(node)

    def _node_to_string(self, node: ast.AST) -> str:
        try:
            return ast.unparse(node)
        except Exception:
            try:
                return repr(ast.literal_eval(node))
            except Exception:
                return "<dynamic>"


def parse_db_models(models_file: Path) -> List[Dict]:
    text = safe_read_text(models_file)
    if not text.strip():
        return []

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []

    parser = SqlAlchemyModelParser()
    parser.visit(tree)
    return parser.models


# ============================================================
# TREE BUILDERS
# ============================================================

def list_sorted_children(directory: Path, allowed_extensions: Optional[set] = None) -> List[Path]:
    children = []
    for item in directory.iterdir():
        if item.name.startswith("."):
            continue
        if item.is_dir():
            children.append(item)
        elif item.is_file():
            if allowed_extensions is None or item.suffix in allowed_extensions:
                children.append(item)

    children.sort(key=lambda p: (not p.is_dir(), p.name.lower()))
    return children


def build_backend_tree(root: Path) -> str:
    lines = [f"BACKEND: {root}"]

    def walk(current: Path, prefix: str = "") -> None:
        children = list_sorted_children(current, BACKEND_EXTENSIONS)

        for i, child in enumerate(children):
            is_last = i == len(children) - 1
            lines.append(prefix + tree_prefix(is_last) + child.name)

            next_prefix = prefix + child_prefix(is_last)

            if child.is_dir():
                walk(child, next_prefix)
            else:
                functions, routes = parse_backend_file(child)

                details = []
                if routes:
                    details.append("ROUTES")
                    details.extend([f"  - {r}" for r in routes])

                if functions:
                    details.append("FUNCTIONS")
                    details.extend([f"  - {f}" for f in functions])

                for j, detail in enumerate(details):
                    detail_is_last = j == len(details) - 1
                    lines.append(next_prefix + tree_prefix(detail_is_last) + detail)

    walk(root)
    return "\n".join(lines)


def build_frontend_tree(root: Path, included_dirs: List[str]) -> str:
    lines = [f"FRONTEND: {root}"]

    included_paths = [root / d for d in included_dirs if (root / d).exists()]

    def walk(current: Path, prefix: str = "") -> None:
        children = list_sorted_children(current, FRONTEND_EXTENSIONS)

        for i, child in enumerate(children):
            is_last = i == len(children) - 1
            lines.append(prefix + tree_prefix(is_last) + child.name)

            next_prefix = prefix + child_prefix(is_last)

            if child.is_dir():
                walk(child, next_prefix)
            else:
                parsed = parse_frontend_file(child)

                details = []
                for item in parsed["types"]:
                    details.append(f"  - {item}")
                for item in parsed["export_functions"]:
                    details.append(f"  - {item}")
                for item in parsed["consts"]:
                    details.append(f"  - {item}")
                for item in parsed["lets"]:
                    details.append(f"  - {item}")

                for j, detail in enumerate(details):
                    detail_is_last = j == len(details) - 1
                    lines.append(next_prefix + tree_prefix(detail_is_last) + detail)

    for idx, included_path in enumerate(included_paths):
        is_last_root = idx == len(included_paths) - 1
        lines.append(tree_prefix(is_last_root) + included_path.name)
        walk(included_path, child_prefix(is_last_root))

    return "\n".join(lines)


def build_models_section(models_file: Path) -> str:
    models = parse_db_models(models_file)
    lines = [f"DB MODELS: {models_file}"]

    if not models:
        lines.append("└── No models found or file could not be parsed")
        return "\n".join(lines)

    for i, model in enumerate(models):
        is_last_model = i == len(models) - 1
        lines.append(tree_prefix(is_last_model) + f"{model['class_name']}")

        model_prefix = child_prefix(is_last_model)

        table_name = model["table_name"] or "<no __tablename__>"
        sections = [
            ("TABLE", [f"  - {table_name}"]),
            ("COLUMNS", [f"  - {c}" for c in model["columns"]] or ["  - none"]),
            ("RELATIONSHIPS", [f"  - {r}" for r in model["relationships"]] or ["  - none"]),
        ]

        for j, (section_title, section_items) in enumerate(sections):
            is_last_section = j == len(sections) - 1
            lines.append(model_prefix + tree_prefix(is_last_section) + section_title)

            section_prefix = model_prefix + child_prefix(is_last_section)
            for k, item in enumerate(section_items):
                is_last_item = k == len(section_items) - 1
                lines.append(section_prefix + tree_prefix(is_last_item) + item.strip())

    return "\n".join(lines)


# ============================================================
# MAIN
# ============================================================

def validate_paths() -> None:
    problems = []

    if not Path(BACKEND_DIR).exists():
        problems.append(f"BACKEND_DIR not found: {BACKEND_DIR}")

    if not Path(FRONTEND_DIR).exists():
        problems.append(f"FRONTEND_DIR not found: {FRONTEND_DIR}")

    if not Path(DB_MODELS_FILE).exists():
        problems.append(f"DB_MODELS_FILE not found: {DB_MODELS_FILE}")

    if problems:
        raise FileNotFoundError("\n".join(problems))


def main() -> None:
    validate_paths()

    backend_root = Path(BACKEND_DIR)
    frontend_root = Path(FRONTEND_DIR)
    db_models_file = Path(DB_MODELS_FILE)

    sections = [
        "=" * 80,
        "PROJECT ARCHITECTURE MAP",
        "=" * 80,
        "",
        build_backend_tree(backend_root),
        "",
        "=" * 80,
        "",
        build_frontend_tree(frontend_root, FRONTEND_INCLUDED_DIRS),
        "",
        "=" * 80,
        "",
        build_models_section(db_models_file),
        "",
    ]

    output = "\n".join(sections)
    Path(OUTPUT_FILE).write_text(output, encoding="utf-8")

    print(f"Done. Architecture map saved to: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()