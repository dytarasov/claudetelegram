"""Словарь терминов: чистая замена в домене и живой словарь в сервисе.

Главное, что тут проверяется, — не «чинит ли он клаудикод», а то, что замена не
трогает обычные слова. Она работает вслепую по всей расшифровке, и цена ошибки —
испорченный текст там, где человек ничего чинить не просил.
"""
from __future__ import annotations

import pytest

from app.domain.models import SttTerm
from app.domain.terms import SEED_TERMS, TermIndex
from app.services.terms import TermsService


# --------------------------------------------------------------------------- #
#  чистая замена
# --------------------------------------------------------------------------- #
@pytest.fixture
def index():
    return TermIndex(SEED_TERMS)


@pytest.mark.parametrize("heard,expected", [
    ("клаудикод", "Claude Code"),
    ("клауди код", "Claude Code"),
    ("клод-код", "Claude Code"),
    ("пайтон", "Python"),
    ("постгресс", "PostgreSQL"),
    ("пеговектор", "pgvector"),
    ("эмбейдинг", "эмбеддинг"),
])
def test_known_variants_are_fixed(index, heard, expected):
    assert index.apply(heard)[0] == expected


def test_longer_variant_wins_over_shorter(index):
    """«клауди код» целиком, а не «клауди» плюс осиротевшее «код»."""
    assert index.apply("запусти клауди код")[0] == "запусти Claude Code"


@pytest.mark.parametrize("text", ["питончик свернулся", "гитара расстроена",
                                  "апифания", "лампочка", "клаудикодер"])
def test_ordinary_words_are_untouched(index, text):
    assert index.apply(text) == (text, [])


def test_case_is_ignored_but_result_is_canonical(index):
    assert index.apply("Клаудикод и ПАЙТОН")[0] == "Claude Code и Python"


def test_replacements_are_reported(index):
    _, applied = index.apply("клаудикод на пайтон")
    assert [str(r) for r in applied] == ["клаудикод → Claude Code", "пайтон → Python"]


def test_empty_dictionary_is_harmless():
    assert TermIndex({}).apply("любой текст") == ("любой текст", [])


def test_no_variant_belongs_to_two_terms():
    seen: dict[str, str] = {}
    for canonical, variants in SEED_TERMS.items():
        for variant in variants:
            assert seen.setdefault(variant.lower(), canonical) == canonical, variant


# --------------------------------------------------------------------------- #
#  живой словарь
# --------------------------------------------------------------------------- #
class FakeTermRepository:
    def __init__(self, broken: bool = False):
        self.items: list[SttTerm] = []
        self.broken = broken

    async def add(self, term):
        self.items = [t for t in self.items if t.variant.lower() != term.variant.lower()]
        term.id = len(self.items) + 1
        self.items.append(term)
        return term

    async def remove(self, variant):
        before = len(self.items)
        self.items = [t for t in self.items if t.variant.lower() != variant.lower()]
        return len(self.items) < before

    async def all(self):
        if self.broken:
            raise RuntimeError("база лежит")
        return list(self.items)


async def test_learned_word_is_fixed_without_restart():
    service = TermsService(FakeTermRepository())
    assert (await service.normalize("залей в кубер"))[0] == "залей в кубер"

    await service.learn("Kubernetes", ["кубер"])
    fixed, applied = await service.normalize("залей в кубер")
    assert fixed == "залей в Kubernetes"
    assert str(applied[0]) == "кубер → Kubernetes"


async def test_forgetting_a_word_takes_effect_immediately():
    service = TermsService(FakeTermRepository())
    await service.learn("Kubernetes", ["кубер"])
    assert await service.forget("кубер") is True
    assert (await service.normalize("залей в кубер"))[0] == "залей в кубер"


async def test_seed_still_works_when_database_is_down():
    """Без базы словарь худеет до затравки, но голосовые продолжают работать."""
    service = TermsService(FakeTermRepository(broken=True))
    assert (await service.normalize("поставь пайтон"))[0] == "поставь Python"


async def test_learning_the_same_variant_twice_relearns_it():
    repo = FakeTermRepository()
    service = TermsService(repo)
    await service.learn("Kubernetes", ["кубер"])
    await service.learn("k8s", ["кубер"])
    assert len(repo.items) == 1
    assert (await service.normalize("кубер"))[0] == "k8s"


async def test_mapping_merges_seed_and_learned():
    service = TermsService(FakeTermRepository())
    await service.learn("Claude Code", ["клавикод"])
    mapping = await service.mapping()
    assert "клавикод" in mapping["Claude Code"]
    assert "клаудикод" in mapping["Claude Code"]
