"""界面验证：工时组合「装配结构」页 —— 方向键升降层级 / 调顺序 + 一键归位。

跑在 8000/5173 两个开发服务上，后端指向 data/verify_ui.db（生产库副本），
所以这里敲的每个键都不会落到真实台账里。
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5173"
API = "http://127.0.0.1:8000"
SHOTS = Path(__file__).resolve().parent.parent / "logs" / "shots"
SHOTS.mkdir(parents=True, exist_ok=True)

PASS: list[str] = []
FAIL: list[str] = []


def check(cond, msg: str) -> None:
    (PASS if cond else FAIL).append(msg)
    print(("  OK   " if cond else "  FAIL ") + msg)


def get_token() -> str:
    req = urllib.request.Request(
        f"{API}/api/auth/login",
        data=json.dumps({"username": "admin", "password": "admin123"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())["token"]


def node_chain(page, name: str) -> list[str]:
    """某一行在树里的祖先链（含自己），用来确认层级真的变了。"""
    return page.evaluate(
        """(name) => {
      const names = [...document.querySelectorAll('.combo-tree .tn-name')];
      const hit = names.find((n) => n.textContent.trim() === name);
      if (!hit) return null;
      let el = hit.closest('.el-tree-node');
      const out = [];
      while (el) {
        const own = el.querySelector(':scope > .el-tree-node__content .tn-name');
        if (own) out.unshift(own.textContent.trim());
        el = el.parentElement ? el.parentElement.closest('.el-tree-node') : null;
      }
      return out;
    }""",
        name,
    )


def api_parent(part: str) -> str:
    with urllib.request.urlopen(f"{API}/api/combos/tree?product=YUJ-18BZ&structure=1") as r:
        tree = json.loads(r.read())

    def walk(nodes, parent=""):
        for n in nodes:
            if n.get("part") == part:
                return parent
            got = walk(n.get("children") or [], n.get("part") or "")
            if got is not None:
                return got
        return None

    for p in tree["products"]:
        for root in p["roots"]:
            got = walk([root])
            if got is not None:
                return got
    return ""


def row(page, name: str):
    """按整名精确命中一行（「料仓」不能顺手点到「料仓水套」）。"""
    pat = re.compile(f"^{re.escape(name)}$")
    return page.locator(".combo-tree .tn-name").filter(has_text=pat).first


def main() -> int:
    token = get_token()
    with sync_playwright() as pw:
        # 这台机器上 playwright 自带的 chromium 版本对不上，直接用系统 Edge
        browser = pw.chromium.launch(channel="msedge")
        page = browser.new_page(viewport={"width": 1500, "height": 900})
        page.goto(f"{BASE}/#/login")
        page.evaluate("(t) => localStorage.setItem('wt_token', t)", token)
        page.goto(f"{BASE}/#/combos?view=tree")
        page.wait_for_selector(".combo-tree", timeout=20000)

        # 只看 18BZ 这一台，树短、点得准
        page.fill(".tools input", "YUJ-18BZ")
        page.keyboard.press("Enter")
        page.wait_for_timeout(1500)
        page.click("text=全部展开")
        page.wait_for_timeout(800)
        page.screenshot(path=str(SHOTS / "100_structure.png"))

        check("点中一个件后" in page.inner_text(".tree-tip"), "工具条说明了方向键用法")
        check(page.locator(".tn-name").count() > 20, f"树里有 {page.locator('.tn-name').count()} 行")
        chain = node_chain(page, "刀具Ф5.0")
        check(chain == ["YUJ-18BZ", "整机装配", "刀具Ф5.0"], f"初始位置 {' › '.join(chain or [])}")

        # ↑↓：选中「料仓」，往上挪一位
        row(page, "料仓").click()
        page.wait_for_timeout(300)
        check(page.locator(".tn-focus").count() == 1, "点中的行有选中态")
        check(page.locator(".tree-focus").is_visible(), "顶部显示当前位置")
        page.screenshot(path=str(SHOTS / "101_focus.png"))

        before = page.evaluate(
            """() => [...document.querySelectorAll('.combo-tree .tn-name')].map(n => n.textContent.trim())"""
        )
        page.keyboard.press("ArrowUp")
        page.wait_for_timeout(1500)
        after = page.evaluate(
            """() => [...document.querySelectorAll('.combo-tree .tn-name')].map(n => n.textContent.trim())"""
        )
        check(
            after.index("料仓") == before.index("料仓") - 1,
            f"↑ 把「料仓」往前挪了一位（{before.index('料仓')} → {after.index('料仓')}）",
        )

        # →：把「刀具Ф5.0」挂到「刀具」下
        row(page, "刀具Ф5.0").click()
        page.wait_for_timeout(300)
        page.keyboard.press("ArrowRight")
        page.wait_for_timeout(1600)
        chain = node_chain(page, "刀具Ф5.0")
        check(chain and chain[-2:] == ["刀具", "刀具Ф5.0"], f"→ 之后挂进刀具（{' › '.join(chain or [])}）")
        check(api_parent("刀具Ф5.0") == "刀具", "接口侧也确认落在「刀具」下")
        page.screenshot(path=str(SHOTS / "102_indent.png"))

        # ←：退回来
        page.keyboard.press("ArrowLeft")
        page.wait_for_timeout(1600)
        chain = node_chain(page, "刀具Ф5.0")
        check(chain and chain[-1] == "刀具Ф5.0" and chain[-2] != "刀具", f"← 之后退回上一级（{' › '.join(chain or [])}）")
        page.screenshot(path=str(SHOTS / "103_outdent.png"))

        # 一键归位：预演清单
        page.click("text=按件名一键归位")
        page.wait_for_selector(".an-list", timeout=15000)
        page.wait_for_timeout(600)
        rows = page.locator(".an-row").count()
        check(rows > 1, f"预演列出了 {rows - 1} 条会被调整的关系")
        check("刀具Ф5.0" in page.inner_text(".an-list"), "预演里有「刀具Ф5.0」→「刀具」")
        page.screenshot(path=str(SHOTS / "104_autonest.png"))

        # 取消一行勾选，应用按钮上的件数要跟着减 —— 批量改动得能一条条放行
        btn = page.locator(".el-dialog__footer button", has_text="应用")
        before_n = int(re.search(r"\d+", btn.inner_text()).group())
        page.locator(".an-row").nth(1).locator(".el-checkbox").click()
        page.wait_for_timeout(300)
        after_n = int(re.search(r"\d+", btn.inner_text()).group())
        check(after_n == before_n - 1, f"去掉一条勾选后应用件数 {before_n} → {after_n}")
        page.screenshot(path=str(SHOTS / "105_autonest_pick.png"))
        page.click(".el-dialog__footer >> text=取消")

        # 清单视图里的「结构」跳转
        page.click("text=清单")
        page.wait_for_timeout(1200)
        page.fill(".tools input", "刀具Ф5.0")
        page.keyboard.press("Enter")
        page.wait_for_timeout(1200)
        page.locator("tbody tr", has_text="刀具Ф5.0").first.locator("text=结构").click()
        page.wait_for_timeout(1800)
        check(page.locator(".tree-focus").is_visible(), "清单点「结构」能跳到树上并定位")
        page.screenshot(path=str(SHOTS / "106_locate.png"))

        browser.close()

    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for x in FAIL:
        print("  FAIL " + x)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
