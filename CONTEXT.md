# Agent Session Storage

`asdu` describes local conversation storage without conflating the application that owns a session with the model runtime used inside it.

## Language

**Source**:
The agent or harness that owns the session format and controls how the session is resumed.
_Avoid_: Provider, backend

**Session type**:
The session's role within its source, such as a main session or child agent.
_Avoid_: Source, provider

**Session lineage**:
The recorded relationship between stored sessions. A child agent and a
user-created fork can both have parents without having the same session type.
_Avoid_: Message parent, folder nesting

**Storage state**:
Whether the source considers a session active or archived. This is separate from
session type; archived Codex sessions are shown as `arch` in the compact list.

**Session title**:
A display name taken from the source when available, otherwise derived from the
session's goal or conversation. A native user-assigned name takes precedence.
_Avoid_: Summary, first request

**Source adapter**:
The boundary that translates one source's native storage and lifecycle into
sessions.
_Avoid_: Plugin, provider

**Session action**:
A reviewed lifecycle operation supported by the source that owns a session.
Codex provides archive, restore, and delete. Claude session files can move to
the system Trash.

**Runtime agent**:
A live or background execution associated with a stored session. Its short ID
and status belong to the source runtime, not to the transcript file.
_Avoid_: Session, subagent

**Session command**:
A source-native command for resuming or controlling a session or its runtime
agent. It is guidance, distinct from an action performed by asdu.

**Provider**:
The runtime service used for a model request. A session may use more than one provider.
_Avoid_: Source, harness

**Model**:
The provider-specific model used for a request.
_Avoid_: Agent, source
