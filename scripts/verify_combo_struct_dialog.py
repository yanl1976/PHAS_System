"""界面验证：工时组合 →「结构」→ 弹窗里调层级与顺序。

用法：python scripts/verify_combo_struct_dialog.py
前提：后端指到库副本（WT_DB_URL=data/verify_ui5.db）、前端 5173 在跑。

盯四件事：
  1. 点「结构」开的是弹窗，页面不再切到树视图；
  2. 弹窗里方向键仍然生效（→ 挂进上一件），并且有「已保存」回执；
  3. 关闭弹窗后台账的「层级 · 装在」跟着变了（说明两边读的是同一份数据）；
  4. 从某一行的「层级 · 装在」点进去，弹窗打开时已经定位到那一件。
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:5173"
API = "http://127.0.0.1:8000"
SHOTS = Path(__file__).resolve().parent.parent / "logs" / "shots"
PROD = "YUJ-18BZ"
# 目标件不写死：当前库里「刀具Ф5.0」可能已经挂在「刀具」下，
# 从根下平铺的件里动态挑「有前一个同级兄弟、自己没有子件」的那一个，
# 才测得到 → 到底往哪挂。
TARGET = ""

PASS = 0
FAIL: list[str] = []


def check(cond, msg: str) -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  OK   {msg}")
    else:
        FAIL.append(msg)
        print(f"  FAIL {msg}")


def get_token() -> str:
    body = json.dumps({"username": "admin", "password": "admin123"}).encode()
    req = urllib.request.Request(
        f"{API}/api/auth/login", data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())["token"]


def chain_of(node: dict, name: str, trail: list[str] | None = None):
    trail = trail or []
    cur = trail + [node.get("part") or ""]
    if (node.get("part") or "") == name:
        return cur
    for k in node.get("children") or []:
        hit = chain_of(k, name, cur)
        if hit:
            return hit
    return None


def api_chain(token: str, name: str):
    req = urllib.request.Request(
        f"{API}/api/combos/tree?product={PROD}&structure=1",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    prod = next((p for p in data["products"] if p["product"] == PROD), None)
    for root in (prod or {}).get("roots") or []:
        hit = chain_of(root, name)
        if hit:
            return hit
    return None


def pick_target(token: str) -> str:
    data = api_tree_raw(token)
    prod = next((p for p in data["products"] if p["product"] == PROD), None)
    for root in (prod or {}).get("roots") or []:
        kids = root.get("children") or []
        for k in kids[1:]:
            if not k.get("children") and k.get("id"):
                return k["part"]
    return ""


def api_tree_raw(token: str) -> dict:
    req = urllib.request.Request(
        f"{API}/api/combos/tree?product={PROD}&structure=1",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def row(page, name: str):
    pat = re.compile(f"^{re.escape(name)}$")
    return page.locator(".sp-tree .tn-name").filter(has_text=pat).first


def main() -> int:
    token = get_token()
    global TARGET
    TARGET = pick_target(token)
    if not TARGET:
        print("  FAIL 找不到可测的件（根下没有第二个平铺件）")
        return 1
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge")
        page = browser.new_page(viewport={"width": 1500, "height": 900})
        page.goto(f"{BASE}/#/login")
        page.evaluate("(t) => localStorage.setItem('wt_token', t)", token)
        page.goto(f"{BASE}/#/combos")
        page.wait_for_selector(".el-table", timeout=20000)
        page.fill(".tools input", PROD)
        page.keyboard.press("Enter")
        page.wait_for_timeout(1500)

        check(page.locator(".sp-tree").count() == 0, "进页面没有树 —— 页面只做台账")
        check(page.locator("text=按产品树").count() == 0, "「按产品树」那个页签已经没有了")

        # ---- 点工具条的「产品结构」
        page.click("text=产品结构")
        page.wait_for_timeout(1500)
        check(page.locator(".el-dialog .sp-tree").count() == 1, "弹窗里有结构树")
        page.screenshot(path=str(SHOTS / "120_struct_dialog.png"))

        # 选到目标产品，展开
        page.click(".sp-tools .el-select")
        page.wait_for_timeout(400)
        page.click(f".el-select-dropdown__item:has-text('{PROD}')")
        page.wait_for_timeout(900)
        page.click(".sp-tools >> text=全部展开")
        page.wait_for_timeout(900)
        check(page.locator(".sp-tree .tn-name").count() > 10, f"树里有 {page.locator('.sp-tree .tn-name').count()} 行")

        start = api_chain(token, TARGET)
        check(start is not None, f"接口侧能看到「{TARGET}」（{' › '.join(start or [])}）")

        # ---- 弹窗里按 →
        row(page, TARGET).click()
        page.wait_for_timeout(400)
        page.keyboard.press("ArrowRight")
        page.wait_for_timeout(1800)
        after = api_chain(token, TARGET)
        check(after and after != start, f"弹窗里按 → 生效（{' › '.join(after or [])}）")
        chip = page.locator(".el-dialog .sv-chip.done")
        check(chip.count() == 1, "弹窗里有「已保存」回执")
        check(bool(re.search(r"\d{2}:\d{2}:\d{2}", chip.inner_text() if chip.count() else "")), "回执带时间")
        page.screenshot(path=str(SHOTS / "121_struct_dialog_move.png"))

        # ---- 弹窗里撤销
        page.click(".el-dialog .sv-undo")
        page.wait_for_timeout(1800)
        back = api_chain(token, TARGET)
        check(back == start, f"弹窗里撤销回到原位（{' › '.join(back or [])}）")

        # ---- 关掉弹窗，台账的「装在」要跟着变（先改一次再看）
        page.keyboard.press("ArrowRight")
        page.wait_for_timeout(1800)
        page.click(".el-dialog__headerbtn")
        page.wait_for_timeout(1500)
        check(page.locator(".el-dialog .sp-tree").count() == 0, "弹窗已关闭")
        line = page.locator(".el-table__row").filter(has_text=TARGET).first
        check(line.count() == 1, "台账里能找到这一行")
        cell = line.locator(".lv-cell")
        if line.count():
            txt = cell.inner_text()
            check(
                "子件" in txt and "装在" in txt,
                f"台账「层级 · 装在」已跟着变（{txt.replace(chr(10), ' ')}）",
            )
        page.screenshot(path=str(SHOTS / "122_list_after_change.png"))

        # ---- 点「层级 · 装在」这一格，弹窗打开就定位到这一件
        cell.click()
        page.wait_for_timeout(2000)
        check(page.locator(".el-dialog .sp-tree").count() == 1, "点这一格打开的也是弹窗")
        focus_txt = page.locator(".el-dialog .sp-focus").inner_text()
        check(TARGET in focus_txt, f"弹窗打开时已定位到这一件（{focus_txt.replace(chr(10), ' ')}）")
        page.screenshot(path=str(SHOTS / "123_dialog_located.png"))

        # 复原：撤销栈跟着弹窗实例走（关掉再开是新的），所以这里直接调接口退回去，
        # 保证副本库跑完还是原样
        body = json.dumps({"product": PROD, "part": TARGET, "action": "outdent"}).encode()
        req = urllib.request.Request(
            f"{API}/api/combos/move",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        )
        urllib.request.urlopen(req, timeout=15)
        final = api_chain(token, TARGET)
        check(final == start, f"末尾回到起始位置（{' › '.join(final or [])}）")

        browser.close()

    print(f"\n通过 {PASS} 项，失败 {len(FAIL)} 项")
    for x in FAIL:
        print(f"  FAIL {x}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
