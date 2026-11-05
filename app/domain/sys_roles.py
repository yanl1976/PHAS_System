"""系统角色（权限上限）与岗位职能（具体授权）。

## 三层，别混

这套系统里「角色」有两个、再加上「职能」，一共三层，**各自回答不同问题**：

| 层 | 存在哪 | 取值 | 回答什么 |
| --- | --- | --- | --- |
| **系统角色** | `app_user.role` | admin / editor / viewer | 这个账号在系统里能走到哪一步（能不能写、能不能管账号/备份） |
| **岗位角色** | `role` 表 + `worker_role` | 工作者 / 检验员 / 计划员… | 这个人在车间是什么身份、能不能进识别名册 |
| **岗位职能** | `role.duties` | 任务编制 / 工艺卡编制 / 报工 / 工时单价 | 这个岗位能承担哪几件**系统功能** |

一句话：**系统角色定上限，岗位职能定边界，岗位角色定身份**。

- 一个人可以是检验员，但根本没有账号 —— 他不录单，只是单据上签名。
- 一个账号可以是只读，对应的人却是老师傅 —— 他只是进来看台账。
- 兼岗很常见：同一人既编制生产任务又编制工艺卡，就把两个岗位都挂上，
  职能取**并集**。这正是「一人多岗」要表达的东西。

所以账号上不存岗位：`app_user.worker_id` 指向名册里的人，
岗位与职能由 `worker_role` 那边给出。

## 判定顺序（`require_perm` 就是这么写的）

1. **管理员直接放行**。管理员是运维身份，不该因为没挂岗位就做不了事。
2. **职能类权限**（任务编制/工艺卡编制/报工/工时单价）→ 看这个人的岗位职能并集。
3. **非职能类权限**（查看、录单等其它写操作、基础数据、账号、备份）→ 看系统角色。

## 「没配职能」怎么办

`DEFAULT_DUTIES_BY_ROLE` 决定未配置时的兜底：editor 默认拿任务编制 + 工艺卡编制 + 报工
（等价于改造前的行为），**不含**工时单价。一旦该人的任一岗位勾了职能，就**严格按岗位并集**
判定，没勾的那项立即失效 —— 这是「必须由专用岗位执行」能落地的前提。

只读账号即使挂了岗位也不会提权：它的默认职能集是空的，岗位勾得再多也进不了第一步。

## 系统角色的硬边界（这张表必须与代码一致）

| 能力 | admin | editor | viewer |
| --- | :-: | :-: | :-: |
| 查看全部页面与数据 | ✓ | ✓ | ✓ |
| 录单 / 核对 / 编排 / 报工 / 任务 | ✓ | ✓ | ✗ |
| 基础数据（件名 / 工序 / 材料 / 产品 / 人员 / 岗位角色） | ✓ | ✓ | ✗ |
| 工时单价 | ✓ | ✗ | ✗ |
| 账号管理 | ✓ | ✗ | ✗ |
| 备份 / 恢复 | ✓ | ✗ | ✗ |

editor 故意保留基础数据的写权限：新件名、新工序出现时，计划员得当场能加，
否则识别对不上就得停下来找管理员——这种"权限正确但活干不下去"的设计没人会用。

viewer 的只读是**真的拦**（见 `app/main.py` 的中间件）：写请求一律 403，
不只是界面上把按钮变灰。只读账号还能改自己密码，否则密码过期就死锁了。
"""
from __future__ import annotations

# 权限点：代码里判断权限只认这些常量，别写字符串字面量。
PERM_READ = "read"                    # 查看
PERM_BUSINESS = "business"            # 录单/核对/编排/报工/任务
PERM_BASE_DATA = "base_data"          # 基础数据（字典、人员、岗位角色）
PERM_WAGE_RATE = "wage_rate"          # 工时单价
PERM_ACCOUNT = "account"              # 账号管理
PERM_BACKUP = "backup"                # 备份与恢复

# 权限点的人类说法：界面上的矩阵直接按这个顺序与措辞渲染，
# 免得文档写一套、界面写一套、代码又是一套。
PERM_LABELS: tuple[tuple[str, str], ...] = (
    (PERM_READ, "查看页面与数据"),
    (PERM_BUSINESS, "写操作总闸（只读账号由服务端直接拒绝）"),
    (PERM_BASE_DATA, "基础数据（件名 / 工序 / 材料 / 产品 / 人员 / 岗位角色）"),
    (PERM_WAGE_RATE, "工时单价"),
    (PERM_ACCOUNT, "账号管理"),
    (PERM_BACKUP, "备份与恢复"),
)

SYSTEM_ROLES: tuple[dict, ...] = (
    {
        "code": "admin",
        "name": "管理员",
        "summary": "全部权限，含账号、备份与单价",
        "description": "管账号、改基础数据、备份恢复、定工时单价，也能做全部业务操作。",
        "perms": (
            PERM_READ,
            PERM_BUSINESS,
            PERM_BASE_DATA,
            PERM_WAGE_RATE,
            PERM_ACCOUNT,
            PERM_BACKUP,
        ),
    },
    {
        "code": "editor",
        "name": "录入核对",
        "summary": "业务与基础数据可写，不能管账号/备份/单价",
        "description": "计划员、核对员。能录单、核对、编排工艺、报工、维护基础数据；不能建账号、备份恢复、定单价。",
        "perms": (PERM_READ, PERM_BUSINESS, PERM_BASE_DATA),
    },
    {
        "code": "viewer",
        "name": "只读",
        "summary": "只能看，写操作被服务端拒绝",
        "description": "看台账、看工资、看工艺卡。任何改动都会被服务端拒绝（只能改自己的密码）。",
        "perms": (PERM_READ,),
    },
)

SYSTEM_ROLE_CODES: tuple[str, ...] = tuple(r["code"] for r in SYSTEM_ROLES)
_BY_CODE: dict[str, dict] = {r["code"]: r for r in SYSTEM_ROLES}


# ------------------------------------------------------------------ 岗位职能
# 只有这几件事按岗位授权：它们直接对应「谁有权让系统里产生/改变生产数据」。
# 基础数据（件名/工序/材料）故意不在里面：新件名新工序出现时计划员得当场能加，
# 走岗位授权会让人卡在「权限对但活干不下去」。备份与账号属运维，只给管理员。
DUTY_TASK_PLAN = "task_plan"        # 生产任务编制
DUTY_ROUTE_EDIT = "route_edit"      # 工艺卡编制（工艺路线与工时）
DUTY_REPORT = "report"              # 报工（录单 / 核对 / 扫码 / 台账改数）
DUTY_WAGE_RATE = PERM_WAGE_RATE     # 工时单价与工资核算

# 顺序即界面上的勾选顺序，也是人读的顺序：先排产、再编工艺、再报工、最后算钱。
DUTY_LABELS: tuple[tuple[str, str], ...] = (
    (DUTY_TASK_PLAN, "生产任务编制"),
    (DUTY_ROUTE_EDIT, "工艺卡编制"),
    (DUTY_REPORT, "报工（录单 / 核对 / 扫码）"),
    (DUTY_WAGE_RATE, "工时单价与工资核算"),
)
DUTY_CODES: tuple[str, ...] = tuple(c for c, _ in DUTY_LABELS)
DUTY_LABEL_MAP: dict[str, str] = dict(DUTY_LABELS)

# 每项职能实际管住哪些接口 —— 界面照抄，不再手写一份说明书。
DUTY_SCOPES: dict[str, str] = {
    DUTY_TASK_PLAN: "建任务 / 改任务 / 挂工艺路线 / 确认版本",
    DUTY_ROUTE_EDIT: "建改工艺路线、编工时、发布与作废、工时组合与产品结构",
    DUTY_REPORT: "改报工单、核对、扫码报工、生成预印单、补录维度",
    DUTY_WAGE_RATE: "定工时单价、生成与导出工资表",
}

# 未配置岗位职能时的兜底。editor 保持改造前的行为（业务三类），不含工时单价。
DEFAULT_DUTIES_BY_ROLE: dict[str, frozenset[str]] = {
    "admin": frozenset(DUTY_CODES),
    "editor": frozenset({DUTY_TASK_PLAN, DUTY_ROUTE_EDIT, DUTY_REPORT}),
    "viewer": frozenset(),
}


def is_duty(perm: str | None) -> bool:
    return (perm or "") in DUTY_LABEL_MAP


def duty_label(code: str) -> str:
    return DUTY_LABEL_MAP.get(code, code)


def normalize_duties(raw) -> tuple[str, ...]:
    """把库里的逗号串 / 请求里的数组统一成**合法且去重**的职能元组。

    不认识的值直接丢掉而不是报错：岗位职能是配置项，写坏一个让人整个岗位失效
    更难查。丢掉的会在返回值里体现（调用方可以对比长度提示）。
    """
    if raw is None:
        return ()
    if isinstance(raw, str):
        items = [x.strip() for x in raw.replace("，", ",").split(",")]
    else:
        items = [str(x).strip() for x in raw]
    seen: list[str] = []
    for it in items:
        if it in DUTY_LABEL_MAP and it not in seen:
            seen.append(it)
    # 按 DUTY_CODES 的顺序输出，界面与库里都稳定
    return tuple(c for c in DUTY_CODES if c in seen)


def duties_of_role(codes) -> tuple[str, ...]:
    """给一组职能码，返回规范化后的元组（同一个入口，避免各处自己 split）。"""
    return normalize_duties(codes)


def default_duties(role_code: str | None) -> frozenset[str]:
    return DEFAULT_DUTIES_BY_ROLE.get(role_code or "", frozenset())


def duty_matrix() -> dict:
    """岗位职能的勾选项清单，给界面渲染。"""
    return {
        "duties": [
            {"code": c, "label": lb, "scope": DUTY_SCOPES.get(c, "")} for c, lb in DUTY_LABELS
        ],
        "default_by_role": {k: sorted(v) for k, v in DEFAULT_DUTIES_BY_ROLE.items()},
    }


# 默认角色：新建账号不指定时用它。
DEFAULT_SYSTEM_ROLE = "editor"


def is_system_role(code: str | None) -> bool:
    return (code or "") in _BY_CODE


def system_role(code: str | None) -> dict:
    """取一个系统角色的定义；未知角色按最小权限（只读）返回。

    为什么不抛异常：库里的 role 可能来自老版本或被脚本改坏，
    这时候让人登不进来是更糟的结果 —— 降级成只读，能看总比 500 好。
    """
    return _BY_CODE.get(code or "", _BY_CODE["viewer"])


def system_role_name(code: str | None) -> str:
    return system_role(code)["name"]


def can(code: str | None, perm: str) -> bool:
    return perm in system_role(code)["perms"]


def perm_label(perm: str) -> str:
    return dict(PERM_LABELS).get(perm, perm)


def deny_message(code: str | None, perm: str) -> str:
    """403 里给操作员看的话。

    不要只说「无权限」：车间里的人看到这四个字只会去找管理员，管理员也不知道
    是该改角色还是该换账号。所以直接说清「你是什么角色、缺哪一项、该找谁」。
    """
    role = system_role(code)
    return (
        f"当前账号的系统角色是「{role['name']}」，没有「{perm_label(perm)}」权限。"
        f"如需这项操作，请让管理员在「系统管理 → 账号」里调整系统角色。"
    )


def duty_deny_message(role_code: str | None, perm: str, positions: list[str] | None = None) -> str:
    """职能不够时的 403 文案。

    和 `deny_message` 分开写，因为该找的地方不一样：系统角色不够要去改账号，
    职能不够要去给**岗位**勾职能（改账号没用）。只说「无权限」会让人白跑一趟。
    """
    have = "、".join(positions or []) or "未分配岗位"
    return (
        f"「{duty_label(perm)}」是岗位职能，当前账号（系统角色「{system_role_name(role_code)}」，"
        f"岗位：{have}）的岗位上没有勾选这一项。请让管理员在「系统管理 → 岗位角色」里"
        f"给对应岗位勾上「{duty_label(perm)}」，或把你调整到能承担这项工作的岗位。"
    )


def matrix() -> dict:
    """给界面用的整张矩阵：系统角色 × 权限点，外加岗位职能的勾选项。"""
    return {
        # 工时单价已经从「系统角色能力」变成「岗位职能」，不再出现在上面这张表里：
        # 两处都列一遍，读者会去系统角色那边找开关，而它其实在岗位上。
        "perms": [{"code": c, "label": lb} for c, lb in PERM_LABELS if not is_duty(c)],
        "roles": [
            {
                "code": r["code"],
                "name": r["name"],
                "summary": r["summary"],
                "description": r["description"],
                "perms": list(r["perms"]),
                "default_duties": sorted(default_duties(r["code"])),
            }
            for r in SYSTEM_ROLES
        ],
        "default": DEFAULT_SYSTEM_ROLE,
        **duty_matrix(),
    }
