"""会话 / 消息的 CRUD 与短期记忆适配（详细设计 3.2.4 / 2.6 / 4.3.1）。

包含两个职责：

1. 会话与消息的事务边界（`seq` 在事务内分配，唯一约束 `(conversation_id, seq)` 兜底，1.5.5）；
2. `SqlShortTermMemory`：ORM 版短期记忆，同时实现 `MessageSource`（读）与
   `ShortTermMemory`（`build` / `append` / `complete`），是 runtime 与 DB 之间唯一的适配点（1.2）。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import MessageRole
from app.core.errors import ConversationNotFoundError, MessageNotFoundError, ValidationError
from app.core.logging import get_logger
from app.db.models import Agent, Conversation, Message
from app.db.models.conversation import CONVERSATION_STATUS_ACTIVE, OWNER_KEY_LOCAL, TITLE_MAX_CHARS
from app.runtime.agent.state import AgentSpec
from app.runtime.llm.base import ChatMessage, ToolCallSpec, dump_arguments
from app.runtime.llm.usage import TokenUsage
from app.runtime.memory.base import MemoryConfig, StoredMessage
from app.runtime.memory.short_term import ShortTermConfig, trim_window
from app.schemas.conversation import ConversationCreate, ConversationUpdate

logger = get_logger(__name__)

MESSAGE_PAGE_LIMIT = 50
"""消息列表默认倒序取最近 50 条（3.2.4）。"""

MESSAGE_PAGE_MAX = 200


async def list_conversations(
    session: AsyncSession,
    *,
    agent_id: str | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[Sequence[Conversation], int]:
    """偏移分页（3.1：配置类资源用 `?page=&page_size=`），返回 `(rows, total)`。"""
    filters = []
    if agent_id:
        filters.append(Conversation.agent_id == agent_id)
    if q:
        filters.append(Conversation.title.contains(q))

    total = (await session.execute(select(func.count(Conversation.id)).where(*filters))).scalar_one()
    statement = (
        select(Conversation)
        .where(*filters)
        .order_by(Conversation.updated_at.desc())
        .offset(max(0, page - 1) * page_size)
        .limit(page_size)
    )
    return (await session.execute(statement)).scalars().all(), int(total)


async def get_conversation(session: AsyncSession, conversation_id: str) -> Conversation:
    """取会话；不存在 → `CONVERSATION_NOT_FOUND`（404）。"""
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        raise ConversationNotFoundError(f"Conversation '{conversation_id}' does not exist")
    return conversation


async def create_conversation(session: AsyncSession, data: ConversationCreate) -> Conversation:
    """创建会话（3.2.4）；`agent_id` 必须存在。"""
    agent = await session.get(Agent, data.agent_id)
    if agent is None or agent.deleted_at is not None:
        raise ValidationError(f"Agent '{data.agent_id}' does not exist", details={"field": "agent_id"})
    conversation = Conversation(
        agent_id=data.agent_id,
        title=(data.title or "").strip(),
        owner_key=OWNER_KEY_LOCAL,
        status=CONVERSATION_STATUS_ACTIVE,
    )
    session.add(conversation)
    await session.commit()
    await session.refresh(conversation)
    return conversation


async def update_conversation(session: AsyncSession, conversation_id: str, data: ConversationUpdate) -> Conversation:
    """重命名 / 归档（3.2.4）。"""
    conversation = await get_conversation(session, conversation_id)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(conversation, field, value)
    await session.commit()
    await session.refresh(conversation)
    return conversation


async def delete_conversation(session: AsyncSession, conversation_id: str) -> None:
    """物理删除（2.2：只有 `agents` / `knowledge_bases` 用软删除）；消息与 Run 级联删除。"""
    conversation = await get_conversation(session, conversation_id)
    await session.delete(conversation)
    await session.commit()


async def list_messages(
    session: AsyncSession,
    conversation_id: str,
    *,
    limit: int = MESSAGE_PAGE_LIMIT,
    cursor: int | None = None,
) -> tuple[Sequence[Message], int | None]:
    """游标分页（3.1：`messages` 用 `?limit=&cursor=`），**倒序**返回最近的消息。

    `cursor` = 上一页最早一条的 `seq`；返回的 `next_cursor` 为 `None` 表示没有更多历史。
    """
    await get_conversation(session, conversation_id)
    page_limit = max(1, min(limit, MESSAGE_PAGE_MAX))
    statement = (
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.seq.desc())
        .limit(page_limit + 1)
    )
    if cursor is not None:
        statement = statement.where(Message.seq < cursor)
    rows = list((await session.execute(statement)).scalars().all())
    next_cursor = None
    if len(rows) > page_limit:
        rows = rows[:page_limit]
        next_cursor = rows[-1].seq
    return rows, next_cursor


async def get_message(session: AsyncSession, message_id: str) -> Message:
    """取消息；不存在 → `MESSAGE_NOT_FOUND`（404，2.6）。"""
    message = await session.get(Message, message_id)
    if message is None:
        raise MessageNotFoundError(f"Message '{message_id}' does not exist")
    return message


async def append_message(
    session: AsyncSession,
    *,
    conversation_id: str,
    role: MessageRole,
    content: str = "",
    run_id: str | None = None,
    name: str | None = None,
    tool_calls: Sequence[dict[str, object]] = (),
    tool_call_id: str | None = None,
    meta: dict[str, object] | None = None,
) -> Message:
    """追加一行消息：`seq` 在事务内分配，并维护会话的冗余计数（2.6）。"""
    seq = (
        await session.execute(
            select(func.coalesce(func.max(Message.seq), 0) + 1).where(Message.conversation_id == conversation_id)
        )
    ).scalar_one()
    now = datetime.now(UTC).replace(tzinfo=None)
    message = Message(
        conversation_id=conversation_id,
        run_id=run_id,
        seq=int(seq),
        role=str(role),
        content=content,
        name=name,
        tool_calls=list(tool_calls),
        tool_call_id=tool_call_id,
        meta=dict(meta or {}),
    )
    session.add(message)
    await session.flush()
    await _touch_conversation(session, conversation_id, role=role, content=content, now=now)
    await session.commit()
    await session.refresh(message)
    return message


async def _touch_conversation(
    session: AsyncSession, conversation_id: str, *, role: MessageRole, content: str, now: datetime
) -> None:
    """维护 `message_count` / `last_message_at`，标题为空时取首条用户消息前 30 字（2.6）。"""
    values: dict[str, object] = {"last_message_at": now, "updated_at": now}
    if role == MessageRole.USER:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is not None and not conversation.title:
            values["title"] = content.strip()[:TITLE_MAX_CHARS]
    await session.execute(
        update(Conversation)
        .where(Conversation.id == conversation_id)
        .values(message_count=Conversation.message_count + 1, **values)
    )


class SqlShortTermMemory:
    """ORM 版短期记忆（4.3.1 + Phase 1 的三处签名细化）。

    - `build` → `recent_messages` + `trim_window`（window 策略，纯函数在 `runtime/memory/short_term.py`）；
    - `append` → 落一行占位 assistant 消息，返回 `message_id`（供 `message.started` / `delta`）；
    - `complete` → 流式结束后回填内容、用量、耗时与 `finish_reason`。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---- MessageSource（读） ----
    async def recent_messages(
        self, conversation_id: str, *, limit: int, before_seq: int | None = None
    ) -> list[StoredMessage]:
        statement = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.seq.desc())
            .limit(max(1, limit))
        )
        if before_seq is not None:
            statement = statement.where(Message.seq < before_seq)
        rows = (await self._session.execute(statement)).scalars().all()
        return [_to_stored(row) for row in reversed(list(rows))]

    # ---- ShortTermMemory（读 + 写） ----
    async def build(
        self,
        conversation_id: str,
        *,
        agent: AgentSpec,
        before_seq: int | None = None,
    ) -> list[ChatMessage]:
        config = ShortTermConfig.from_config(MemoryConfig.from_agent(agent).short_term)
        stored = await self.recent_messages(
            conversation_id,
            limit=config.max_turns * 3,
            before_seq=before_seq,
        )
        window = trim_window(stored, max_turns=config.max_turns, max_tokens=config.max_tokens)
        return [item.to_chat_message() for item in window]

    async def append(self, conversation_id: str, message: ChatMessage, *, run_id: str | None = None) -> str:
        """落一行消息并返回其 id（4.1.1 的 `append` 细化）。"""
        row = await append_message(
            self._session,
            conversation_id=conversation_id,
            role=message.role,
            content=message.content,
            run_id=run_id,
            name=message.name,
            tool_calls=[_tool_call_payload(call) for call in message.tool_calls],
            tool_call_id=message.tool_call_id,
        )
        return row.id

    async def complete(
        self,
        message_id: str,
        *,
        content: str,
        finish_reason: str,
        usage: TokenUsage,
        latency_ms: int,
        model_name: str,
        error_code: str | None = None,
        tool_calls: Sequence[ToolCallSpec] = (),
    ) -> None:
        """回填流式结果（4.1.1 的 `complete` 细化）。

        `tool_calls` 非空时同时回填 `messages.tool_calls`（4.4.3：`assistant(tool_calls) → tool`
        的交替必须能落库还原）；纯对话行不动该列，保持 Phase 1 行为不变。
        """
        row = await get_message(self._session, message_id)
        row.content = content
        row.finish_reason = finish_reason
        row.prompt_tokens = usage.prompt_tokens
        row.completion_tokens = usage.completion_tokens
        row.total_tokens = usage.total_tokens
        row.latency_ms = latency_ms
        row.model_name = model_name
        row.error_code = error_code
        if tool_calls:
            row.tool_calls = [_tool_call_payload(call) for call in tool_calls]
        await self._session.commit()


def _to_stored(row: Message) -> StoredMessage:
    """`messages` 行 → runtime 投影（2.6 的 `tool_calls` 结构）。"""
    return StoredMessage(
        seq=row.seq,
        role=MessageRole(row.role),
        content=row.content or "",
        message_id=row.id,
        tool_calls=tuple(_tool_call_spec(item) for item in (row.tool_calls or [])),
        tool_call_id=row.tool_call_id,
        name=row.name,
    )


def _tool_call_spec(item: dict[str, object]) -> ToolCallSpec:
    arguments = item.get("arguments")
    parsed = dict(arguments) if isinstance(arguments, dict) else {}
    return ToolCallSpec(
        id=str(item.get("id") or ""),
        name=str(item.get("name") or ""),
        arguments=parsed,
        raw_arguments=str(item.get("raw_arguments") or dump_arguments(parsed)),
    )


def _tool_call_payload(call: ToolCallSpec) -> dict[str, object]:
    """`ToolCallSpec` → 2.6 约定的 `tool_calls` 元素。"""
    return {"id": call.id, "name": call.name, "arguments": dict(call.arguments)}
