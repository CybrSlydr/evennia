"""
Tests for the transaction-aware identity map (utils/idmapper/transactional.py): a write rolled back in the
database is rolled back in memory too, every cached object then agrees with its row, and an instance whose
creation was rolled back can't be saved.

Each test runs inside the test case's own transaction, so each `atomic()` block below is a savepoint: the
rollback path the patch handles. A restart after a commit (a fresh process reading committed state) needs a real
commit, and was proven outside the suite (Campaign Desk's proof, 2026-10-31).

"""

from django.db import connection, transaction

from evennia.objects.models import ObjectDB
from evennia.objects.objects import DefaultObject
from evennia.utils import create
from evennia.utils.idmapper import transactional
from evennia.utils.test_resources import BaseEvenniaTest


class Rollback(Exception):
    pass


class TestTransactional(BaseEvenniaTest):
    def rolled_back(self, change):
        """Run change() in an atomic block that then fails."""
        with self.assertRaises(Rollback):
            with transaction.atomic():
                change()
                raise Rollback()

    def assertConsistent(self):
        """Every cached object agrees with its row, and every cached contents list with the rows it holds."""
        for obj in ObjectDB.get_all_cached_instances():
            if getattr(obj, "_tx_dead", False) or not obj.pk:
                continue
            with connection.cursor() as cur:
                cur.execute("SELECT db_location_id, db_key FROM objects_objectdb WHERE id=%s", [obj.pk])
                row = cur.fetchone()
            self.assertIsNotNone(row, f"#{obj.pk} is cached with no row")
            memory = (obj.__dict__.get("db_location_id"), obj.__dict__.get("db_key"))
            self.assertEqual(memory, row, f"#{obj.pk}: memory, then disk")
            if "contents_cache" in obj.__dict__:
                with connection.cursor() as cur:
                    cur.execute("SELECT id FROM objects_objectdb WHERE db_location_id=%s", [obj.pk])
                    held = sorted(r[0] for r in cur.fetchall())
                self.assertEqual(sorted(x.pk for x in obj.contents), held, f"#{obj.pk}'s contents")

    def make(self, key, location):
        return create.create_object(DefaultObject, key=key, location=location, home=self.room1)

    def test_a_move_and_rename_rolled_back_leave_memory_as_disk(self):
        self.room1.contents, self.room2.contents  # (both contents caches built)

        def change():
            self.obj1.location = self.room2
            self.obj1.key = "Moved"

        self.rolled_back(change)
        self.assertEqual((self.obj1.location, self.obj1.key), (self.room1, "Obj"))
        self.assertIn(self.obj1, self.room1.contents)
        self.assertNotIn(self.obj1, self.room2.contents)
        self.assertConsistent()

    def test_an_attribute_and_a_tag_rolled_back_are_gone_from_memory(self):
        self.obj1.db.mood = "calm"

        def change():
            self.obj1.db.mood = "angry"
            self.obj1.tags.add("marked")

        self.rolled_back(change)
        self.assertEqual(self.obj1.db.mood, "calm")
        self.assertFalse(self.obj1.tags.has("marked"))

    def test_a_creation_rolled_back_leaves_a_dead_instance_that_cannot_be_saved(self):
        self.room1.contents
        made = []
        self.rolled_back(lambda: made.append(self.make("Newborn", self.room1)))
        newborn = made[0]
        self.assertTrue(newborn._tx_dead)
        self.assertIsNone(newborn.pk)
        self.assertNotIn(newborn, self.room1.contents)
        self.assertFalse(ObjectDB.objects.filter(db_key="Newborn").exists())
        replacement = self.make("Replacement", self.room1)  # (it may be given the dead one's id)
        with self.assertRaises(transactional.Dead):
            newborn.save()
        self.assertEqual(ObjectDB.objects.get(pk=replacement.pk).db_key, "Replacement")
        self.assertConsistent()

    def test_a_deletion_rolled_back_brings_the_object_back(self):
        self.obj1.db.counter = 5
        pk = self.obj1.pk
        self.room1.contents
        self.rolled_back(self.obj1.delete)
        self.assertTrue(ObjectDB.objects.filter(pk=pk).exists())
        self.assertEqual(self.obj1.pk, pk)
        self.assertEqual(self.obj1.db.counter, 5)
        self.assertIn(self.obj1, self.room1.contents)
        self.assertConsistent()

    def test_a_holder_deleted_and_rolled_back_keeps_its_contents(self):
        """Deleting a holder moves its contents first (Evennia's clear_contents): the rollback undoes both."""
        bag = self.make("pouch", self.char1)
        gem, coin = self.make("gem", bag), self.make("coin", bag)
        self.room1.contents, self.char1.contents, bag.contents
        self.rolled_back(bag.delete)
        self.assertEqual((gem.location, coin.location), (bag, bag))
        self.assertEqual(sorted(o.key for o in bag.contents), ["coin", "gem"])
        self.assertIn(bag, self.char1.contents)
        self.assertConsistent()

    def test_a_merge_rolled_back_puts_everything_back(self):
        bag = self.make("pouch", self.char1)
        gem, coin = self.make("gem", bag), self.make("coin", bag)
        self.char1.contents, bag.contents
        made = []

        def change():
            gem.location = self.char1
            sack = self.make("sack", self.char1)
            made.append(sack)
            coin.location = sack
            bag.delete()

        self.rolled_back(change)
        self.assertEqual((gem.location, coin.location), (bag, bag))
        self.assertTrue(made[0]._tx_dead)
        self.assertEqual(sorted(o.key for o in self.char1.contents), ["pouch"])
        self.assertConsistent()

    def test_an_inner_block_rolled_back_leaves_the_outer_blocks_changes(self):
        with transaction.atomic():
            self.rolled_back(lambda: setattr(self.obj1, "location", self.room2))
            self.obj2.key = "Kept"
        self.assertEqual(self.obj1.location, self.room1)
        self.assertEqual(self.obj2.key, "Kept")
        self.assertEqual(ObjectDB.objects.get(pk=self.obj2.pk).db_key, "Kept")
        self.assertConsistent()

    def test_a_block_that_commits_keeps_its_changes(self):
        with transaction.atomic():
            self.obj1.location = self.room2
            self.obj1.db.shine = "bright"
        self.assertEqual(self.obj1.location, self.room2)
        self.assertEqual(ObjectDB.objects.get(pk=self.obj1.pk).db_location_id, self.room2.pk)
        self.assertEqual(self.obj1.db.shine, "bright")
        self.assertConsistent()
