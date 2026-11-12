"""再生スレッドの回収（PlaybackController.wait_for_players）を確かめる。"""

import asyncio
from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.audio.playback import PlaybackController


class WaitForPlayersTests(unittest.IsolatedAsyncioTestCase):
    def start_player(self) -> tuple[threading.Thread, threading.Event]:
        release = threading.Event()
        player = threading.Thread(target=release.wait, daemon=True)
        player.start()
        self.addCleanup(release.set)
        self.addCleanup(player.join, 2)
        return player, release

    async def test_waits_for_every_player_and_forgets_them(self):
        controller = PlaybackController()
        first, release_first = self.start_player()
        second, release_second = self.start_player()
        controller._players.update({first, second})
        task = asyncio.create_task(controller.wait_for_players())
        await asyncio.sleep(0.05)
        self.assertFalse(task.done())
        release_first.set()
        release_second.set()
        await asyncio.wait_for(task, 2)
        self.assertEqual(len(controller._players), 0)

    async def test_cancellation_still_collects_finished_and_remaining_players(self):
        """キャンセルされても、終わったスレッドを記録から外し、残りのスレッドも待ち終えてから伝える（#432）。"""
        controller = PlaybackController()
        first, release_first = self.start_player()
        second, release_second = self.start_player()
        controller._players.update({first, second})
        task = asyncio.create_task(controller.wait_for_players())
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.sleep(0.05)
        self.assertFalse(task.done(), "スレッドが終わるまで所有権を返さない")
        release_first.set()
        release_second.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(len(controller._players), 0)


if __name__ == "__main__":
    unittest.main()
