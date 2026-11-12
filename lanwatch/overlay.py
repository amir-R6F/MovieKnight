"""Countdown overlay shown before playback starts.

A centred card with a very large number ("3", "2", "1", "GO"). The timing is
driven by ``tkinter.after``, as required, and every scheduled callback is
tracked so a pause/stop/resync can cancel the whole thing instantly.

The transport row stays visible below the card, and clicking the card cancels
the countdown; so do the Pause/Space shortcuts. A countdown you cannot abort
would be a bad joke.
"""

from __future__ import annotations

import math
from typing import Callable

import customtkinter as ctk

BACKGROUND = "#0b1220"
NUMBER_COLOR = "#f9fafb"
GO_COLOR = "#4ade80"
CAPTION_COLOR = "#9ca3af"


class CountdownOverlay(ctk.CTkFrame):
    """A self-contained overlay card that counts down and then hides itself."""

    def __init__(
        self,
        master: ctk.CTkBaseClass,
        on_finished: Callable[[], None] | None = None,
        on_cancel: Callable[[], None] | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(
            master,
            width=360,
            height=280,
            corner_radius=24,
            fg_color=BACKGROUND,
            border_width=2,
            border_color="#334155",
            **kwargs,
        )
        self.grid_propagate(False)
        self.pack_propagate(False)

        self._on_finished = on_finished
        self._on_cancel = on_cancel
        self._after_ids: list[str] = []
        self._active = False

        self._number = ctk.CTkLabel(
            self,
            text="3",
            font=ctk.CTkFont(size=132, weight="bold"),
            text_color=NUMBER_COLOR,
            width=300,
            height=190,
        )
        self._number.pack(pady=(18, 0))

        self._caption = ctk.CTkLabel(
            self,
            text="Get ready…",
            font=ctk.CTkFont(size=15),
            text_color=CAPTION_COLOR,
        )
        self._caption.pack(pady=(0, 18))

        # A click anywhere on the card aborts the countdown.
        for widget in (self, self._number, self._caption):
            widget.bind("<Button-1>", self._handle_click)

    # --- public API ----------------------------------------------------------

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def current_text(self) -> str:
        return str(self._number.cget("text"))

    def start(self, seconds: float) -> None:
        """Show the countdown for *seconds* and hide it shortly after GO."""
        self.cancel()

        try:
            remaining = max(0.0, float(seconds))
        except (TypeError, ValueError):
            remaining = 0.0

        self._active = True
        self.place(relx=0.5, rely=0.5, anchor="center")
        self.lift()

        first = max(1, math.ceil(remaining)) if remaining > 0 else 0
        self._number.configure(
            text=str(first) if first else "GO", text_color=NUMBER_COLOR
        )
        self._caption.configure(text="Get ready…")

        if remaining <= 0.0:
            self._schedule(0.0, self._show_go)
            self._schedule(0.75, self.hide)
            return

        # "3" stays until one second is left, "2" until two are gone, and "1"
        # covers the final second. With a shortened first step (a late
        # delivery, say) the early numbers collapse onto t=0.
        for number in (3, 2, 1):
            delay = max(0.0, remaining - float(number))
            self._schedule(delay, lambda value=number: self._show_number(value))
        self._schedule(remaining, self._show_go)
        self._schedule(remaining + 0.75, self.hide)

    def cancel(self) -> None:
        """Hide immediately and drop every pending callback."""
        self._clear_after()
        self._active = False
        self.place_forget()

    def hide(self) -> None:
        """Hide because the countdown finished (reported through on_finished)."""
        self._clear_after()
        was_active = self._active
        self._active = False
        self.place_forget()
        if was_active and self._on_finished is not None:
            try:
                self._on_finished()
            except Exception:  # pragma: no cover - UI callback
                pass

    def destroy(self) -> None:  # type: ignore[override]
        self._clear_after()
        super().destroy()

    # --- internals -----------------------------------------------------------

    def _handle_click(self, _event: object = None) -> None:
        if not self._active:
            return
        self.cancel()
        if self._on_cancel is not None:
            try:
                self._on_cancel()
            except Exception:  # pragma: no cover - UI callback
                pass

    def _show_number(self, number: int) -> None:
        if not self._active:
            return
        self._number.configure(text=str(number), text_color=NUMBER_COLOR)
        self._caption.configure(text="Get ready…")

    def _show_go(self) -> None:
        if not self._active:
            return
        self._number.configure(text="GO", text_color=GO_COLOR)
        self._caption.configure(text="Playing…")

    def _schedule(self, delay: float, callback: Callable[[], None]) -> None:
        milliseconds = max(0, int(round(delay * 1000)))
        try:
            after_id = self.after(milliseconds, callback)
        except Exception:  # pragma: no cover - widget already destroyed
            return
        self._after_ids.append(after_id)

    def _clear_after(self) -> None:
        for after_id in self._after_ids:
            try:
                self.after_cancel(after_id)
            except Exception:
                pass
        self._after_ids.clear()
