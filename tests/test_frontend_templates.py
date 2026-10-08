"""
前端模板的结构性检查
=====================

**为什么需要它**：Vue 对"模板里用了未导入的组件"只打一条 **warning**，
`vite build` 照样成功、控制台也没有 error —— 但那个组件**一个元素都不会渲染**。

真实事故：知识库页"切块设置"卡片里，三个 `<UiInput>` 都没被 import
（`KnowledgeView.vue` 漏了 `import UiInput`），于是「块大小」「块间重叠」
「自定义分隔符」三个输入框在界面上**只剩标签、没有输入框**，用户根本没法填。
构建通过、测试全绿、控制台干净 —— 直到用户截图过来说"你这个都没给框我咋填"。

一条几行的静态检查就能提前发现它，所以放在这里当作回归护栏。
"""

import re
from pathlib import Path

# 从测试文件推到仓库根，再进前端源码
FRONTEND_SRC = Path(__file__).resolve().parent.parent / "frontend" / "src"

# 只检查项目自有的 UI 组件（`Ui` 前缀）；原生标签与本文件内的子组件不管
_TAG = re.compile(r"<(Ui[A-Za-z]+)[\s/>]")
_IMPORT = re.compile(r"import\s+(Ui[A-Za-z]+)\s+from")


def _vue_files() -> list[Path]:
    assert FRONTEND_SRC.is_dir(), f"前端源码目录不存在: {FRONTEND_SRC}"
    return sorted(FRONTEND_SRC.rglob("*.vue"))


class TestUiComponentsAreImported:
    def test_src_tree_exists(self):
        """前置断言：目录找不到时不要"通过"，否则这条护栏会静默失效。"""
        assert _vue_files(), f"{FRONTEND_SRC} 下没有 .vue 文件，检查路径是否失效"

    def test_every_ui_component_used_in_a_template_is_imported(self):
        problems: list[str] = []

        for path in _vue_files():
            text = path.read_text(encoding="utf-8")
            m = re.search(r"<template>(.*)</template>", text, re.S)
            if not m:
                continue
            used = set(_TAG.findall(m.group(1)))
            imported = set(_IMPORT.findall(text))
            for name in sorted(used - imported):
                rel = path.relative_to(FRONTEND_SRC.parent.parent)
                problems.append(
                    f"{rel}: 模板里用了 <{name}> 但没有 import —— "
                    f"Vue 只会打 warning，构建照样成功，而它一个元素都不渲染"
                )

        assert not problems, "发现未导入的组件:\n" + "\n".join(problems)

    def test_no_unused_ui_imports(self):
        """反向：import 了却不用的，通常意味着改模板时漏删（也是噪声）。"""
        problems: list[str] = []

        for path in _vue_files():
            text = path.read_text(encoding="utf-8")
            m = re.search(r"<template>(.*)</template>", text, re.S)
            if not m:
                continue
            used = set(_TAG.findall(m.group(1)))
            imported = set(_IMPORT.findall(text))
            for name in sorted(imported - used):
                rel = path.relative_to(FRONTEND_SRC.parent.parent)
                problems.append(f"{rel}: import 了 {name} 但模板里没用")

        assert not problems, "发现多余的组件 import:\n" + "\n".join(problems)
