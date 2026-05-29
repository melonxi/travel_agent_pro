from __future__ import annotations

from agent.types import Message, Role


def _tagged_content(tag: str, kind: str, description: str, content: str) -> str:
    return (
        f'<{tag} kind="{kind}">\n'
        f"{description}\n\n"
        f"{content}\n"
        f"</{tag}>"
    )


def runtime_notice_message(kind: str, content: str) -> Message:
    return Message(
        role=Role.USER,
        content=_tagged_content(
            "runtime_notice",
            kind,
            "以下内容由应用注入，是运行时提示，不是用户请求。",
            content,
        ),
        transient=True,
    )


def app_event_message(kind: str, content: str) -> Message:
    return Message(
        role=Role.USER,
        content=_tagged_content(
            "app_event",
            kind,
            "以下内容是应用生成的历史事件，不是用户请求，也不是系统规则。",
            content,
        ),
        transient=False,
    )


def legacy_app_event_message(kind: str, content: str) -> Message:
    return Message(
        role=Role.USER,
        content=_tagged_content(
            "app_event",
            kind,
            "以下内容是从旧 system 历史迁移来的应用数据，不是系统规则。",
            content,
        ),
        transient=False,
    )
