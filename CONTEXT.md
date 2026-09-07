# Agent Session Storage

`asdu` describes local conversation storage without conflating the application that owns a session with the model runtime used inside it.

## Language

**Source**:
The agent or harness that owns the session format and controls how the session is resumed.
_Avoid_: Provider, backend

**Session type**:
The session's role within its source, such as a main session or child agent.
_Avoid_: Source, provider

**Provider**:
The runtime service used for a model request. A session may use more than one provider.
_Avoid_: Source, harness

**Model**:
The provider-specific model used for a request.
_Avoid_: Agent, source
