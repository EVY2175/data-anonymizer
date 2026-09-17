"""
Абстракция identity mapping для формализованных идентификаторов
(ИНН/КПП/ОГРН) — Stage 7B.3.

======================================================================
Почему это НЕ MappingStore
======================================================================

IdentifierMappingStore архитектурно похож на MappingStore (app.mapping.base)
там, где это действительно полезно (add с идемпотентным точным дублем и
конфликтом, O(1) lookup по identity, snapshot-семантика all_tokens()/
entries()), но сознательно НЕ является его обобщением и НЕ разделяет с ним
общий базовый класс/exception:

    - identity здесь ДВУХкомпонентная — (identifier_type, identifier_value),
      без parent_alias: у идентификаторов нет иерархии, и добавление
      parent_alias-подобного поля создало бы неправильную identity, которую
      потом нельзя тихо "исправить" (та же причина, по которой Stage 7A не
      трогает parent_alias для идентификаторов);
    - здесь нет partial/ambiguous lookup: `get_by_identity` всегда требует
      ОБА компонента identity явно — в отличие от MappingStore.get_by_real_value,
      здесь никогда не бывает сценария "искать без указания типа", поэтому
      никакого sentinel-параметра и AmbiguousMappingError не нужно;
    - Stage 2 MappingStore/MappingConflictError/AmbiguousMappingError не
      имеют общего базового exception-класса между собой (оба — напрямую
      Exception) — тот же принцип применён и здесь: IdentifierMappingConflictError
      ни от чего специального не наследуется, отдельного
      "IdentifierMappingStoreError" не создано, поскольку у Stage 2 нет
      аналогичной полезной иерархии, которую стоило бы скопировать.

======================================================================
Что этот модуль НЕ делает
======================================================================

Не выполняет: encrypted persistence, JSON-сериализацию, работу с файлами,
crypto, atomic writes (это Stage 7B.4); не интегрируется с anonymizer.py
(Stage 7B.5); не хранит provenance/Excel-координаты (sheet/row/column) —
это будущий, отдельно спроектированный слой; не генерирует token (Stage 7B.2,
app.security.alias_generator.generate_identifier_token); не выполняет
domain-валидацию (checksum ИНН/КПП/ОГРН — Stage 5, app.detectors) —
IdentifierMappingEntry уже гарантирует структурную валидность полей
(Stage 7B.1), а store ничего не проверяет сверх этого.

======================================================================
identifier_value — EXACT, без нормализации
======================================================================

Как и IdentifierMappingEntry (app.models.identifiers), store не выполняет
strip()/casefold()/NFC-нормализацию/int-round-trip/удаление ведущих нулей
над identifier_value — используется ровно то значение, что было передано.
"7701234567" и " 7701234567 " — разные identity; "0770123456" и
"770123456" — разные identity. Это прямое продолжение exact-value
контракта, установленного в Stage 7A/7B.1/7B.2.

======================================================================
Безопасность: identifier_value никогда не попадает в текст исключений
======================================================================

IdentifierMappingConflictError сообщает о конфликте через token
(псевдоним — безопасно раскрывать) и identifier_type (категория, не
конфиденциальна), но никогда не включает в сообщение сырое значение
идентификатора (реальный ИНН/КПП/ОГРН). Это отличается от
MappingConflictError (Stage 2), который включает real_value в сообщение —
там это осознанно приемлемо для названий компаний/сотрудников, но не
приемлемо для государственных идентификационных номеров.
"""

from __future__ import annotations

import abc
from collections.abc import Iterable
from typing import Optional

from app.models.identifiers import IdentifierMappingEntry, IdentifierType


class IdentifierMappingConflictError(Exception):
    """
    Попытка добавить в IdentifierMappingStore запись, которая противоречит
    уже имеющимся данным: либо тот же token уже связан с другой identity,
    либо та же identity (identifier_type, identifier_value) уже связана с
    другим token.

    Сообщение НИКОГДА не содержит сырое значение идентификатора — только
    token (псевдоним) и identifier_type (категория).
    """


class IdentifierMappingStore(abc.ABC):
    """
    Контракт identity mapping для идентификаторов: token <-> (identifier_type,
    identifier_value).

    identifier_value не считается нормализуемым — сравнение производится
    ровно по тем строкам, что были переданы (см. docstring модуля).

    Полная identity записи — пара (identifier_type, identifier_value); оба
    компонента обязательны при любом lookup по identity, partial lookup не
    поддерживается и не нужен (identifier_type всегда известен заранее —
    из FieldRule/IdentifierType на вызывающей стороне).
    """

    @abc.abstractmethod
    def add(self, entry: IdentifierMappingEntry) -> None:
        """
        Добавляет запись identifier mapping.

        Повторное добавление ТОЙ ЖЕ (по значению) записи — не ошибка,
        состояние хранилища не меняется (идемпотентность).

        :raises IdentifierMappingConflictError: если token записи уже
            связан с другой identity, либо identity записи
            (identifier_type, identifier_value) уже связана с другим token.
        :raises TypeError: если entry не является IdentifierMappingEntry.
        """

    @abc.abstractmethod
    def add_many(self, entries: Iterable[IdentifierMappingEntry]) -> None:
        """
        Добавляет сразу несколько записей identifier mapping атомарно
        (all-or-nothing) — Stage 7B.4.3.

        entries материализуется ровно один раз (list/tuple/generator/
        one-shot iterator принимаются одинаково); если сам entries
        поднимает исключение во время материализации, хранилище остаётся
        полностью неизменным (материализация происходит раньше любой
        мутации состояния).

        Пустой entries — no-op.

        Точный дубликат (тот же token и та же identity, что уже есть в
        хранилище либо ранее в этом же batch) — no-op для этой записи,
        не конфликт.

        Если хотя бы одна запись batch конфликтует (тот же token с другой
        identity, либо та же identity с другим token — против уже
        существующего состояния ИЛИ против другой записи этого же batch),
        поднимается IdentifierMappingConflictError и НИ ОДНА запись batch
        не применяется — хранилище остаётся в точности таким, каким было
        до вызова.

        Если весь batch применяется успешно, все его новые записи
        становятся видны одновременно.

        :raises IdentifierMappingConflictError: если любая запись batch
            конфликтует с уже существующими данными или с другой записью
            этого же batch. Сообщение не отличается от сообщения при
            обычном add() — не включает позицию/индекс записи в batch и
            никогда не включает сырое значение идентификатора.
        :raises TypeError: если entries равен None либо содержит элемент,
            не являющийся IdentifierMappingEntry.
        """

    @abc.abstractmethod
    def get_by_token(self, token: str) -> Optional[IdentifierMappingEntry]:
        """Возвращает запись по token, либо None, если такого token нет."""

    @abc.abstractmethod
    def get_by_identity(
        self, identifier_type: IdentifierType, identifier_value: str
    ) -> Optional[IdentifierMappingEntry]:
        """
        Ищет запись по полной identity (identifier_type, identifier_value).

        Оба аргумента обязательны — partial lookup (например, только по
        identifier_value без identifier_type) не поддерживается.

        :returns: запись, либо None, если такой identity нет.
        """

    @abc.abstractmethod
    def all_tokens(self) -> set[str]:
        """
        Возвращает snapshot всех token, известных хранилищу.

        Возвращённый набор — независимая копия: его изменение не влияет
        на внутреннее состояние хранилища.
        """

    @abc.abstractmethod
    def entries(self) -> tuple[IdentifierMappingEntry, ...]:
        """
        Возвращает snapshot всех записей хранилища.

        IdentifierMappingEntry неизменяем (frozen), а сам возвращаемый
        tuple — независимая копия внутренней коллекции.
        """

    @abc.abstractmethod
    def clear(self) -> None:
        """Полностью очищает хранилище (все внутренние индексы)."""
