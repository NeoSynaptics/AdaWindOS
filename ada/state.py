"""Two-layer state machine: Voice FSM + Executive FSM + overlay states."""

from enum import Enum, auto
from dataclasses import dataclass
from datetime import datetime


class VoiceState(Enum):
    IDLE = auto()               # Not listening, wake word active
    ATTENTIVE = auto()          # Recently active, lower threshold to engage
    LISTENING = auto()          # Capturing user speech
    PROCESSING_FAST = auto()    # GPT-OSS 20B processing, <1s
    SPEAKING = auto()           # TTS output active


class ExecutiveState(Enum):
    IDLE = auto()               # No active background work
    DISPATCHING = auto()        # Sending work to OpenHands/OpenCode
    WAITING_BACKGROUND = auto() # Tasks running, waiting on results
    VALIDATING = auto()         # Checking results through 4 gates


class OverlayState(Enum):
    NONE = auto()
    AWAITING_USER_DECISION = auto()
    DEGRADED = auto()
    ERROR = auto()


# Legal transitions
VOICE_TRANSITIONS: dict[VoiceState, list[VoiceState]] = {
    VoiceState.IDLE: [VoiceState.ATTENTIVE],
    VoiceState.ATTENTIVE: [VoiceState.LISTENING, VoiceState.IDLE],
    VoiceState.LISTENING: [VoiceState.PROCESSING_FAST],
    VoiceState.PROCESSING_FAST: [VoiceState.SPEAKING],
    VoiceState.SPEAKING: [VoiceState.ATTENTIVE, VoiceState.LISTENING],  # LISTENING = barge-in
}

EXECUTIVE_TRANSITIONS: dict[ExecutiveState, list[ExecutiveState]] = {
    ExecutiveState.IDLE: [ExecutiveState.DISPATCHING],
    ExecutiveState.DISPATCHING: [ExecutiveState.WAITING_BACKGROUND, ExecutiveState.IDLE],
    ExecutiveState.WAITING_BACKGROUND: [ExecutiveState.VALIDATING, ExecutiveState.IDLE],
    ExecutiveState.VALIDATING: [ExecutiveState.DISPATCHING, ExecutiveState.IDLE],  # DISPATCHING = retry
}


@dataclass
class SystemState:
    voice: VoiceState = VoiceState.IDLE
    executive: ExecutiveState = ExecutiveState.IDLE
    overlay: OverlayState = OverlayState.NONE
    last_voice_activity: datetime | None = None
    last_user_input: datetime | None = None

    def transition_voice(self, to: VoiceState) -> None:
        allowed = VOICE_TRANSITIONS.get(self.voice, [])
        if to not in allowed and self.overlay == OverlayState.NONE:
            raise IllegalTransition(f"Voice: {self.voice.name} → {to.name}")
        self.voice = to
        if to in (VoiceState.LISTENING, VoiceState.SPEAKING):
            self.last_voice_activity = datetime.now()

    def transition_executive(self, to: ExecutiveState) -> None:
        allowed = EXECUTIVE_TRANSITIONS.get(self.executive, [])
        if to not in allowed and self.overlay == OverlayState.NONE:
            raise IllegalTransition(f"Executive: {self.executive.name} → {to.name}")
        self.executive = to

    def set_overlay(self, overlay: OverlayState) -> None:
        self.overlay = overlay

    def clear_overlay(self) -> None:
        self.overlay = OverlayState.NONE

    @property
    def is_degraded(self) -> bool:
        return self.overlay == OverlayState.DEGRADED

    @property
    def is_live_turn(self) -> bool:
        return self.voice in (
            VoiceState.LISTENING,
            VoiceState.PROCESSING_FAST,
            VoiceState.SPEAKING,
        )

    @property
    def tribe_allowed(self) -> bool:
        """TRIBE v2 is OFF during live turns, ON during idle/attentive."""
        return not self.is_live_turn


class IllegalTransition(Exception):
    pass
