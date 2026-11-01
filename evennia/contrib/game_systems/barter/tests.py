"""
Test the contrib barter system
"""

from mock import Mock

from evennia.commands.default.tests import BaseEvenniaCommandTest
from evennia.utils.create import create_object

from . import barter


class TestBarter(BaseEvenniaCommandTest):
    def setUp(self):
        super().setUp()
        self.tradeitem1 = create_object(key="TradeItem1", location=self.char1)
        self.tradeitem2 = create_object(key="TradeItem2", location=self.char1)
        self.tradeitem3 = create_object(key="TradeItem3", location=self.char2)

    def test_tradehandler_base(self):
        self.char1.msg = Mock()
        self.char2.msg = Mock()
        # test all methods of the tradehandler
        handler = barter.TradeHandler(self.char1, self.char2)
        self.assertEqual(handler.part_a, self.char1)
        self.assertEqual(handler.part_b, self.char2)
        handler.msg_other(self.char1, "Want to trade?")
        handler.msg_other(self.char2, "Yes!")
        handler.msg_other(None, "Talking to myself...")
        self.assertEqual(self.char2.msg.mock_calls[0][1][0], "Want to trade?")
        self.assertEqual(self.char1.msg.mock_calls[0][1][0], "Yes!")
        self.assertEqual(self.char1.msg.mock_calls[1][1][0], "Talking to myself...")
        self.assertEqual(handler.get_other(self.char1), self.char2)
        handler.finish(force=True)

    def test_tradehandler_joins(self):
        handler = barter.TradeHandler(self.char1, self.char2)
        self.assertTrue(handler.join(self.char2))
        self.assertTrue(handler.unjoin(self.char2))
        self.assertFalse(handler.join(self.char1))
        self.assertFalse(handler.unjoin(self.char1))
        handler.finish(force=True)

    def test_tradehandler_offers(self):
        handler = barter.TradeHandler(self.char1, self.char2)
        handler.join(self.char2)
        handler.offer(self.char1, self.tradeitem1, self.tradeitem2)
        self.assertEqual(handler.part_a_offers, [self.tradeitem1, self.tradeitem2])
        self.assertFalse(handler.part_a_accepted)
        self.assertFalse(handler.part_b_accepted)
        handler.offer(self.char2, self.tradeitem3)
        self.assertEqual(handler.list(), ([self.tradeitem1, self.tradeitem2], [self.tradeitem3]))
        self.assertEqual(handler.search("TradeItem2"), self.tradeitem2)
        self.assertEqual(handler.search("TradeItem3"), self.tradeitem3)
        self.assertEqual(handler.search("nonexisting"), None)
        self.assertFalse(handler.finish())  # should fail since offer not yet accepted
        handler.accept(self.char1)
        handler.decline(self.char1)
        handler.accept(self.char2)
        handler.accept(self.char1)  # should trigger handler.finish() automatically
        self.assertEqual(self.tradeitem1.location, self.char2)
        self.assertEqual(self.tradeitem2.location, self.char2)
        self.assertEqual(self.tradeitem3.location, self.char1)

    def test_cmdtrade(self):
        self.call(
            barter.CmdTrade(),
            "Char2 : Hey wanna trade?",
            'You say, "Hey wanna trade?"',
            caller=self.char1,
        )
        self.call(barter.CmdTrade(), "Char decline : Nope!", 'You say, "Nope!"', caller=self.char2)
        self.call(
            barter.CmdTrade(),
            "Char2 : Hey wanna trade?",
            'You say, "Hey wanna trade?"',
            caller=self.char1,
        )
        self.call(barter.CmdTrade(), "Char accept : Sure!", 'You say, "Sure!"', caller=self.char2)
        self.call(
            barter.CmdOffer(),
            "TradeItem3",
            "Your trade action: You offer TradeItem3",
            caller=self.char2,
        )
        self.call(
            barter.CmdOffer(),
            "TradeItem1 : Here's my offer.",
            'You say, "Here\'s my offer."\n  [You offer TradeItem1]',
        )
        self.call(
            barter.CmdAccept(),
            "",
            "Your trade action: You accept the offer. Char2 must now also accept",
        )
        self.call(
            barter.CmdDecline(),
            "",
            "Your trade action: You change your mind, declining the current offer.",
        )
        self.call(
            barter.CmdAccept(),
            ": Sounds good.",
            'You say, "Sounds good."\n' "  [You accept the offer. Char must now also accept.",
            caller=self.char2,
        )
        self.call(
            barter.CmdDecline(),
            ":No way!",
            'You say, "No way!"\n  [You change your mind, declining the current offer.]',
            caller=self.char2,
        )
        self.call(
            barter.CmdOffer(),
            "TradeItem1, TradeItem2 : My final offer!",
            'You say, "My final offer!"\n  [You offer TradeItem1 and TradeItem2]',
        )
        self.call(
            barter.CmdAccept(),
            "",
            "Your trade action: You accept the offer. Char2 must now also accept.",
            caller=self.char1,
        )
        self.call(barter.CmdStatus(), "", "Offered by Char:", caller=self.char2)
        self.tradeitem1.db.desc = "A great offer."
        self.call(barter.CmdEvaluate(), "TradeItem1", "A great offer.")
        self.call(
            barter.CmdAccept(),
            ":Ok then.",
            'You say, "Ok then."\n  [You accept the deal.',
            caller=self.char2,
        )
        self.assertEqual(self.tradeitem1.location, self.char2)
        self.assertEqual(self.tradeitem2.location, self.char2)
        self.assertEqual(self.tradeitem3.location, self.char1)

    def test_a_trade_in_progress_is_saved_and_restored_after_a_restart(self):
        handler = barter.TradeHandler(self.char1, self.char2)
        handler.join(self.char2)
        handler.offer(self.char1, self.tradeitem1)
        handler.offer(self.char2, self.tradeitem3)
        handler.accept(self.char1)
        del self.char1.ndb.tradehandler  # (a restart: what was in memory is gone)
        del self.char2.ndb.tradehandler
        restored = barter.TradeHandler.restore(self.char1)
        self.assertEqual((restored.part_a, restored.part_b), (self.char1, self.char2))
        self.assertEqual(restored.list(), ([self.tradeitem1], [self.tradeitem3]))
        self.assertEqual((restored.part_a_accepted, restored.part_b_accepted), (True, False))
        self.assertIs(self.char2.ndb.tradehandler, restored)
        self.assertTrue(restored.accept(self.char2))  # (the restored trade settles)
        self.assertEqual((self.tradeitem1.location, self.tradeitem3.location), (self.char2, self.char1))
        self.assertIsNone(barter.TradeHandler.restore(self.char1))  # (and its saved state is gone)

    def test_restore_with_no_trade_saved_is_none(self):
        self.assertIsNone(barter.TradeHandler.restore(self.char2))

    def test_an_offer_no_longer_held_withdraws_both_acceptances_and_moves_nothing(self):
        handler = barter.TradeHandler(self.char1, self.char2)
        handler.join(self.char2)
        handler.offer(self.char1, self.tradeitem1)
        handler.offer(self.char2, self.tradeitem3)
        self.tradeitem1.location = self.room1  # (given away while the trade waited)
        handler.accept(self.char1)
        self.assertFalse(handler.accept(self.char2))
        self.assertEqual((handler.part_a_accepted, handler.part_b_accepted), (False, False))
        self.assertEqual((self.tradeitem1.location, self.tradeitem3.location), (self.room1, self.char2))
        saved = self.char1.attributes.get(barter.TRADE_STATE)
        self.assertEqual((saved["a_accepted"], saved["b_accepted"]), (False, False))
        handler.finish(force=True)

    def test_a_settlement_that_fails_partway_moves_nothing(self):
        handler = barter.TradeHandler(self.char1, self.char2)
        handler.join(self.char2)
        handler.offer(self.char1, self.tradeitem1, self.tradeitem2)
        handler.offer(self.char2, self.tradeitem3)
        handler.accept(self.char1)
        self.char1.attributes.remove = Mock(side_effect=RuntimeError("fails after the goods moved"))
        try:
            with self.assertRaises(RuntimeError):
                handler.accept(self.char2)
        finally:
            del self.char1.attributes.remove
        self.assertEqual(
            [self.tradeitem1.location, self.tradeitem2.location, self.tradeitem3.location],
            [self.char1, self.char1, self.char2],
        )
        self.assertEqual(
            sorted(o.key for o in self.char2.contents if o.key.startswith("TradeItem")), ["TradeItem3"]
        )
        handler.finish(force=True)

    def test_cmdtradehelp(self):
        self.call(
            barter.CmdTrade(),
            "Char2 : Hey wanna trade?",
            'You say, "Hey wanna trade?"',
            caller=self.char1,
        )
        self.call(barter.CmdTradeHelp(), "", "Trading commands\n", caller=self.char1)
        self.call(
            barter.CmdFinish(),
            ": Ending.",
            'You say, "Ending."\n  [You aborted trade. No deal was made.]',
        )
