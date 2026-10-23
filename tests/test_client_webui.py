"""Structure tests for the single-file web UI.

The client ships one ``index.html`` (no build step, no bundler, no external
assets on purpose). That buys us "unzip and run", but it also means a stray
closing tag silently reparents half the app -- which happened: ``#modal``
ended up inside ``.workspace``, took a grid cell, and pushed the composer off
screen. Nothing caught it until a human noticed the input box was gone.

These tests parse the HTML with the standard library and assert the *shape*
that the CSS depends on. They are cheap and they fail loudly.
"""

from __future__ import annotations

import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

INDEX = (Path(__file__).resolve().parent.parent
         / "markovcryptor" / "client" / "webui" / "index.html")

#: Elements that never need a closing tag.
VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}

#: Tag pairs whose imbalance silently breaks layout. ``<html>``/``<head>``
#: are omitted because browsers tolerate their omission.
CHECKED = {"div", "main", "aside", "section", "header", "footer", "span",
           "button", "body", "template", "ul", "ol", "li"}


class _Tree(HTMLParser):
    """Build a parent/child map, tolerating void and self-closing tags."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parent: dict[int, int] = {}
        self.tag_of: dict[int, str] = {}
        self.attrs_of: dict[int, dict[str, str]] = {}
        self.stack: list[int] = []
        self.next_id = 0
        self.imbalance: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in VOID:
            return
        node = self.next_id
        self.next_id += 1
        self.tag_of[node] = tag
        self.attrs_of[node] = dict(attrs)
        self.parent[node] = self.stack[-1] if self.stack else -1
        self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        return                                  # <x/> never nests

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack:
            self.imbalance.append(f"stray </{tag}>")
            return
        # Pop to the matching opener; anything skipped was never closed.
        for depth in range(len(self.stack) - 1, -1, -1):
            candidate = self.stack[depth]
            if self.tag_of[candidate] == tag:
                for orphan in self.stack[depth + 1:]:
                    self.imbalance.append(f"<{self.tag_of[orphan]}> never closed")
                del self.stack[depth:]
                return
        self.imbalance.append(f"</{tag}> without opener")

    def finish(self):
        self.close()
        for orphan in self.stack:
            self.imbalance.append(f"<{self.tag_of[orphan]}> never closed")


class StructureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = INDEX.read_text(encoding="utf-8")
        tree = _Tree()
        tree.feed(cls.source)
        tree.finish()
        cls.tree = tree
        cls.imbalance = tree.imbalance

    # -- helpers ---------------------------------------------------------

    def _nodes(self, **attrs) -> list[int]:
        out = []
        for node, node_attrs in self.tree.attrs_of.items():
            if all(node_attrs.get(k) == v for k, v in attrs.items()):
                out.append(node)
        return out

    def _by_id(self, element_id: str) -> int:
        found = self._nodes(id=element_id)
        self.assertEqual(len(found), 1,
                         f"#{element_id} 应当恰好出现一次，实际 {len(found)} 次")
        return found[0]

    def _ancestors(self, node: int) -> list[int]:
        chain = []
        current = self.tree.parent.get(node, -1)
        while current != -1:
            chain.append(current)
            current = self.tree.parent.get(current, -1)
        return chain

    def _has_ancestor(self, node: int, element_id: str) -> bool:
        return any(self.tree.attrs_of[a].get("id") == element_id
                   for a in self._ancestors(node))

    def _direct_children(self, node: int) -> list[int]:
        return [n for n, p in self.tree.parent.items() if p == node]

    def _tag(self, node: int) -> str:
        return self.tree.tag_of.get(node, "?")

    # -- tests -----------------------------------------------------------

    def test_tags_are_balanced(self):
        """少一个 </div> 会把后面整块 DOM 吞进去，CSS 就不再匹配。"""
        self.assertEqual(self.imbalance, [],
                         "HTML 标签不配对：" + "; ".join(self.imbalance))

    def test_body_layout_children(self):
        """body 只有两块：workspace + modal（外加不参与布局的 template）。

        身份栏在 aside 里，不再占 body 的一行 —— 右侧内容区因此能顶到
        窗口上沿，两栏贯通。
        <template> 不算：它不参与渲染（内容不在文档流里）。
        """
        body = [n for n, t in self.tree.tag_of.items() if t == "body"]
        self.assertEqual(len(body), 1)
        kids = [k for k in self._direct_children(body[0])
                if self._tag(k) in CHECKED and self._tag(k) != "template"]
        kinds = [f"<{self._tag(k)}"
                 f"{'#' + self.tree.attrs_of[k]['id'] if self.tree.attrs_of[k].get('id') else ''}>"
                 for k in kids]
        self.assertEqual(len(kids), 3,
                         f"body 应当只有 3 个布局元素，实际 {len(kids)}: {kinds}")

    def test_idbar_lives_inside_aside(self):
        """身份栏必须放在左栏里，与列表同宽。

        放回 body 直接子元素会重新变成整宽横条，把右侧内容区从顶部切断。
        """
        idbar = self._by_id("meBlock")
        chain = [self.tree.attrs_of[a].get("class")
                 for a in self._ancestors(idbar)]
        self.assertIn("idbar", chain, "#meBlock 应该在 header.idbar 里")
        self.assertIn("workspace", chain, ".idbar 应该在 .workspace 里")
        # 且必须是 aside 的直接子元素
        idbar_node = next(n for n, t in self.tree.tag_of.items()
                          if t == "header" and "idbar" in self.tree.attrs_of[n].get("class", ""))
        parent = self.tree.parent[idbar_node]
        self.assertEqual(self._tag(parent), "aside",
                         ".idbar 必须是 aside 的直接子元素")

    def test_settings_button_is_in_idbar(self):
        """设置按钮在身份栏里、昵称旁边（不再挂在右上角）。"""
        btn = self._by_id("btnSettings")
        self.assertTrue(self._has_ancestor(btn, "meBlock")
                        or any(self.tree.attrs_of[a].get("class") == "idbar"
                               for a in self._ancestors(btn)),
                        "#btnSettings 应该在 header.idbar 里")

    def test_templates_exist(self):
        """JS 用 clone(id) 取模板；少一个就是整块列表空白。"""
        ids = {self.tree.attrs_of[n].get("id")
               for n, t in self.tree.tag_of.items() if t == "template"}
        for expected in ("tpl-msg", "tpl-row", "tpl-qthumb"):
            with self.subTest(template=expected):
                self.assertIn(expected, ids)
                self.assertNotIn("undefined", ids)

    def test_modal_is_direct_child_of_body(self):
        """#modal 放进 .workspace 会占掉一个 grid 格子，把 #chat 挤走。"""
        modal = self._by_id("modal")
        self.assertFalse(
            self._has_ancestor(modal, "chat") or
            any(self.tree.attrs_of[a].get("class") == "workspace"
                for a in self._ancestors(modal)),
            "#modal 必须是 body 的直接子元素，不能放在 .workspace 里",
        )

    def test_chat_and_empty_share_one_grid_cell(self):
        """#chat 和 #empty 都定位到 grid-column:2 / row:1，必须同父。"""
        chat = self._by_id("chat")
        empty = self._by_id("empty")
        self.assertEqual(self.tree.parent[chat], self.tree.parent[empty],
                         "#chat 与 #empty 必须是兄弟节点（同占一个格子）")
        self.assertFalse(self._has_ancestor(chat, "empty"),
                         "#chat 不能嵌在 #empty 里面")

    def test_chat_lives_inside_workspace_as_direct_child(self):
        chat = self._by_id("chat")
        parent = self.tree.parent[chat]
        self.assertEqual(self.tree.attrs_of[parent].get("class"), "workspace",
                         "#chat 必须是 .workspace 的直接子元素")

    def test_essential_hooks_exist_once(self):
        """JS 用 $("id") 直接取，缺一个就是运行时 null。"""
        for element_id in (
            "meAvatar", "meName", "meUin", "meDot", "btnSettings",
            "tabRecent", "tabFriends", "cntRecent", "cntFriends", "list",
            "chat", "empty", "modal", "input",
        ):
            with self.subTest(element_id=element_id):
                self.assertEqual(len(self._nodes(id=element_id)), 1)


WEBUI = INDEX.parent


class AssetsTest(unittest.TestCase):
    """The single-file UI was split into css/ + js/ modules.

    The split only pays off if it stays a split -- one stray inline <style> or
    a plain <script> tag silently undoes the whole thing.
    """

    @classmethod
    def setUpClass(cls):
        cls.source = INDEX.read_text(encoding="utf-8")

    def test_stylesheets_are_linked_in_order(self):
        """tokens → base → layout → components → overlays.

        后面覆盖前面，乱序就是配色/尺寸错乱，而且很难一眼看出原因。
        """
        hrefs = re.findall(r'<link rel="stylesheet" href="([^"]+)"', self.source)
        expected = [f"css/{n}.css" for n in
                    ("tokens", "base", "layout", "components", "overlays")]
        self.assertEqual(hrefs, expected)

    def test_style_files_exist(self):
        for name in ("tokens", "base", "layout", "components", "overlays"):
            with self.subTest(name=name):
                self.assertTrue((WEBUI / "css" / f"{name}.css").is_file(),
                                f"缺 css/{name}.css")

    def test_no_inline_style_left(self):
        """留一份内联 CSS 就等于有两处要改，改一处必然忘另一处。"""
        self.assertNotIn("<style>", self.source,
                         "index.html 里不该再有内联 <style>：样式都在 css/ 下")

    def test_script_is_a_module(self):
        """ES module 是作用域隔离的前提；改回普通 <script> 会退化成全局污染。"""
        self.assertIn('<script type="module">', self.source)


class ScriptTest(unittest.TestCase):
    """JS 分散在 js/ 下的 ES 模块里；这里检查跨文件的引用完整性。"""

    @classmethod
    def setUpClass(cls):
        cls.sources = {p: p.read_text(encoding="utf-8")
                       for p in sorted(WEBUI.rglob("*.js"))}
        cls.html = INDEX.read_text(encoding="utf-8")
        match = re.search(r'<script type="module">(.*?)</script>', cls.html, re.S)
        cls.sources[INDEX] = match.group(1) if match else ""

    def test_core_modules_exist(self):
        names = {p.relative_to(WEBUI).as_posix() for p in self.sources}
        for expected in ("js/core/dom.js", "js/core/api.js",
                         "js/core/store.js", "js/core/format.js"):
            with self.subTest(module=expected):
                self.assertIn(expected, names, f"缺 {expected}")

    def test_imports_resolve(self):
        """模块间的相对 import 必须指向真实存在的文件。

        浏览器里 import 失败是**静默**的：控制台只有一条 net::ERR，
        页面停在半初始化状态，看起来像"数据没加载"。改文件名忘了改
        import 就会这样，所以这条必须有。
        """
        missing = []
        for path, text in self.sources.items():
            specs = re.findall(
                r"""(?:import|export)[^;]{0,400}?from\s+["']([^"']+)["']""",
                text)
            for spec in specs:
                if not spec.startswith("."):
                    continue
                if not (path.parent / spec).resolve().exists():
                    missing.append(f"{path.name}: {spec}")
        self.assertEqual(missing, [], f"import 指向不存在的文件: {missing}")

    def test_exported_symbols_really_exist(self):
        """``X.y(...)`` 里的 y 必须真的由模块 X 导出。

        import 路径对、但符号名改了（比如 ``install`` → ``installDropper``）
        时，浏览器**不报错**，只在运行时抛 ``TypeError: X.y is not a
        function``。而那句往往在某个启动路径里，页面看着"打开就是空的"，
        很难定位到是改名漏了一处。

        这条实际抓到过 ``Dropper.install is not a function``。
        """
        # 1) 收集每个模块导出的名字
        exports: dict[str, set[str]] = {}
        for path, text in self.sources.items():
            names = set()
            names |= set(re.findall(r"export\s+(?:async\s+)?function\s+(\w+)", text))
            names |= set(re.findall(r"export\s+const\s+(\w+)", text))
            names |= set(re.findall(r"export\s+let\s+(\w+)", text))
            names |= set(re.findall(r"export\s+class\s+(\w+)", text))
            names |= set(re.findall(r"export\s*\{([^}]*)\}", text)
                           and [] or [])
            for group in re.findall(r"export\s*\{([^}]*)\}", text):
                for part in group.split(","):
                    name = part.split(" as ")[-1].strip()
                    if name:
                        names.add(name)
            exports[path.stem] = names

        # 2) 找出每段 module 脚本里 ``Namespace.symbol`` 的用法
        used: dict[str, set[str]] = {}
        pattern = re.compile(
            r"import\s+\*\s+as\s+(\w+)\s+from\s+[\"']([^\"']+)[\"']")
        for alias, spec in pattern.findall(self.html):
            stem = spec.rsplit("/", 1)[-1].removesuffix(".js")
            used[alias] = set(
                re.findall(r"\b" + alias + r"\.(\w+)\b", self.html))
            if stem not in exports:
                continue                      # 由 test_imports_resolve 管
            missing = sorted(used[alias] - exports[stem])
            self.assertEqual(
                missing, [],
                f"{alias}（{stem}.js）没有导出 {missing}；"
                f"该模块导出的是 {sorted(exports[stem])}")


    def test_no_stray_dom_lookup_for_missing_ids(self):
        """每个 $("...") 里的 id 都必须在 HTML 里存在。

        这条抓的是"删了元素忘了删 JS"——运行时表现为 null 报错，
        而且往往在某个不常走的分支里，测试很难碰到。
        """
        ids = set(re.findall(r'\bid="([^"]+)"', self.html))
        used: set[str] = set()
        for text in self.sources.values():
            used |= set(re.findall(r'\$\("([A-Za-z][\w-]*)"\)', text))
        missing = sorted(used - ids)
        self.assertEqual(missing, [],
                         f"JS 引用了不存在的元素 id: {missing}")

    def test_no_redeclared_top_level_symbols(self):
        """同一个顶层符号不能在两个文件里各定义一次。

        拆分时最容易犯的错：把函数搬进新文件却忘了删掉旧的，于是有两份
        实现，改一份不生效，而且没有任何报错。
        """
        seen: dict[str, list[str]] = {}
        for path, text in self.sources.items():
            for name in re.findall(
                    r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)", text, re.M):
                seen.setdefault(name, []).append(path.name)
            for name in re.findall(r"^(?:export\s+)?const\s+(\w+)\s*=", text, re.M):
                seen.setdefault(name, []).append(path.name)
        dupes = {k: sorted(set(v)) for k, v in seen.items() if len(set(v)) > 1}
        self.assertEqual(dupes, {}, f"重复定义的顶层符号: {dupes}")


class UndefinedSymbolTest(unittest.TestCase):
    """每个模块里用到的顶层函数名，必须是它自己定义的或 import 来的。

    实际 bug：视图层改造后 chat.js 和 covert.js 里调了 ``refreshState()``，
    但没 import —— 点开好友才报 ``refreshState is not defined``。
    平时不报，因为那条路径只有"点具体好友"才走到。

    ES module 的作用域是隔离的，这类错误**不会**在页面加载时暴露，
    必须靠静态检查。
    """

    #: JS 内建 + 浏览器全局。不用穷举 —— 漏了最多误报一次，不会漏报。
    GLOBALS = {
        "window", "document", "console", "navigator", "location", "localStorage",
        "sessionStorage", "setTimeout", "clearTimeout", "setInterval",
        "clearInterval", "requestAnimationFrame", "cancelAnimationFrame",
        "fetch", "JSON", "Math", "Date", "Object", "Array", "String", "Number",
        "Boolean", "Promise", "Error", "TypeError", "Map", "Set", "WeakMap",
        "Symbol", "RegExp", "URL", "URLSearchParams", "Image", "File",
        "FileReader", "Blob", "FormData", "AbortController", "Intl",
        "encodeURIComponent", "decodeURIComponent", "parseInt", "parseFloat",
        "isNaN", "isFinite", "structuredClone", "crypto", "performance",
        "history", "screen", "Event", "CustomEvent", "MutationObserver",
        "ResizeObserver", "TextEncoder", "TextDecoder", "undefined",
        "globalThis", "Node", "Element", "HTMLElement", "queueMicrotask",
        "confirm", "alert", "prompt", "Function", "Proxy", "Reflect",
    }

    #: 关键字后面跟括号的写法，不是函数调用。
    KEYWORDS = {
        "if", "for", "while", "switch", "catch", "return", "typeof", "function",
        "await", "new", "delete", "void", "in", "of", "do", "else", "try",
        "finally", "throw", "case", "yield", "async", "with",
        "import",
    }

    @classmethod
    def setUpClass(cls):
        from webui_files import ui_scripts
        cls.sources = ui_scripts()

    def _declared(self, text: str) -> set[str]:
        """本文件声明的顶层名字。"""
        names: set[str] = set()
        names |= set(re.findall(r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)",
                                text, re.M))
        names |= set(re.findall(r"^(?:export\s+)?(?:const|let|var)\s+(\w+)",
                                text, re.M))
        names |= set(re.findall(r"^(?:export\s+)?class\s+(\w+)", text, re.M))
        return names

    def _imported(self, text: str) -> set[str]:
        """import 进来的名字（含 `* as ns` 与解构）。"""
        names: set[str] = set()
        for clause in re.findall(r"^import\s+(.+?)\s+from\s+", text, re.M):
            clause = clause.strip()
            ns = re.match(r"\*\s+as\s+(\w+)", clause)
            if ns:
                names.add(ns.group(1))
                continue
            braces = re.search(r"\{([^}]*)\}", clause)
            if braces:
                for part in braces.group(1).split(","):
                    part = part.strip()
                    if part:
                        names.add(part.split(" as ")[-1].strip())
                continue
            default = re.match(r"(\w+)", clause)
            if default:
                names.add(default.group(1))
        return names

    def _strip_noncode(self, text: str) -> str:
        """去掉注释与字符串字面量，避免把文字内容当代码。

        **顺序很重要**：先处理正则字面量，再处理 ``//`` 注释。
        否则 ``/\.json$/i.test(...)`` 里的斜杠会被当成注释开头，
        把整行前面的 ``const isModelFile = ...`` 一起吞掉，
        于是报一个假阳性。
        """
        # 1) 正则字面量 /.../flags。前面不是标识符/右括号时才算是正则，
        #    这样除法 a / b 不会被误伤。
        text = re.sub(r"(?<![\w)$\]])\/(?:[^\/\\\n]|\\.)+\/[gimsuy]*",
                      "/RE/", text)
        # 2) 字符串
        text = re.sub(r'"(?:[^"\\]|\\.)*"', '""', text)
        text = re.sub(r"'(?:[^'\\]|\\.)*'", "''", text)
        text = re.sub(r"`(?:[^`\\]|\\.)*`", "``", text)
        # 3) 注释（放在字符串之后，避免删掉字符串里的 // 之后又露出来）
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        text = re.sub(r"//[^\n]*", "", text)
        return text

    def _locals(self, text: str) -> set[str]:
        """函数内定义的名字与声明的参数。

        视图模块里大量出现 ``const esc = (e) => ...``、``function toggle()``、
        以及 Promise 回调里的 ``(resolve, reject) =>``。这些都不是顶层符号，
        但对文件来说完全合法。
        """
        names: set[str] = set()
        # 任何位置定义的函数/箭头函数变量
        names |= set(re.findall(r"\bfunction\s+(\w+)", text))
        names |= set(re.findall(r"\b(?:const|let|var)\s+(\w+)\s*=", text))
        names |= set(re.findall(r"\bcatch\s*\(\s*(\w+)", text))
        # 参数列表：(a, b, {c}, d = 1)
        for params in re.findall(r"\(([^()]*)\)\s*(?:=>|\{)", text):
            for raw in params.split(","):
                raw = raw.split("=")[0].strip()
                ident = re.match(r"[\w$]+", raw)
                if ident:
                    names.add(ident.group(0))
        # 单个参数箭头函数： e => ...
        names |= set(re.findall(r"(?<![\w.$])([a-z][A-Za-z0-9_]*)\s*=>", text))
        return names

    def test_no_call_to_undefined_function(self):
        """调用的函数名必须有来源：本文件定义、import、或浏览器全局。"""
        problems = []
        for name, text in self.sources.items():
            available = (self._declared(text) | self._imported(text)
                         | self._locals(text) | self.GLOBALS)
            code = self._strip_noncode(text)
            for m in re.finditer(r"(?<![\w.$])([a-z][A-Za-z0-9_]*)\s*\(", code):
                fn = m.group(1)
                if fn in available or fn in self.KEYWORDS:
                    continue
                # 方法简写定义形如 "  name(" 出现在行首缩进处 —— 那是定义不是调用
                line_start = code.rfind("\n", 0, m.start()) + 1
                prefix = code[line_start:m.start()]
                if not prefix.strip():
                    continue
                problems.append(f"{name}: {fn}()")
        self.assertEqual(sorted(set(problems)), [],
                         "这些函数被调用但没有定义或 import："
                         + ", ".join(sorted(set(problems))))


if __name__ == "__main__":
    unittest.main()
