"""
Абстракция хранилища соответствий «реальное значение <-> псевдоним»
(mapping).

MappingStore не выполняет файловый I/O, ничего не знает о шифровании и не
проверяет связность иерархии (существование parent_alias) — это
исключительно контракт хранения и поиска записей mapping. Конкретные
реализации (InMemoryMappingStore на этом этапе, в будущем —
EncryptedFileMappingStore) подставляются под бизнес-логику
anonymizer/restore, которая не должна знать, как именно mapping хранится.
"""

from __future__ import annotations

import abc
from typing import Optional, Union

from app.models.entities import EntityType, MappingEntry


class MappingConflictError(Exception):
    """
    Попытка добавить в MappingStore запись, которая противоречит уже
    имеющимся данным: либо тот же alias уже связан с другой записью, либо
    та же полная identity (real_value, entity_type, parent_alias) уже
    связана с другим alias.

    Это ошибка целостности данных, а не ошибка входных параметров —
    поэтому отдельный класс, а не ValueError.
    """


class AmbiguousMappingError(Exception):
    """
    get_by_real_value не может однозначно определить единственную запись
    по переданным real_value/entity_type/parent_alias: под эти условия
    подходит более одной записи mapping.

    Это отличается от отсутствия записи (в этом случае возвращается None):
    неоднозначность означает, что кандидатов несколько, и без уточнения
    контекста (entity_type и/или parent_alias) нельзя безопасно выбрать
    один из них — MappingStore никогда не разрешает такую неоднозначность
    произвольным выбором.
    """


class _ParentAliasNotSpecified:
    """
    Приватный sentinel-тип (техническая деталь реализации MappingStore,
    НЕ часть MappingEntry и не часть множества допустимых значений
    parent_alias как таковых).

    Отличает два разных сценария вызова get_by_real_value:
    - parent_alias вообще не передан вызывающим кодом -> "не фильтровать
      по родителю" (значение по умолчанию, см. _PARENT_UNSET ниже);
    - parent_alias передан явно как None -> "искать запись именно без
      родителя (top-level сущность)".

    Вызывающий код никогда не должен создавать эту метку и не должен
    передавать её сам — она работает только как значение параметра по
    умолчанию.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - только для отладочного вывода
        return "<parent_alias not specified>"


# Единственный экземпляр sentinel-типа — используется как значение по
# умолчанию параметра parent_alias в MappingStore.get_by_real_value и во
# всех его реализациях.
_PARENT_UNSET = _ParentAliasNotSpecified()

# Тип параметра parent_alias у get_by_real_value: либо конкретный alias
# родителя (str), либо None (искать запись без родителя), либо sentinel
# "фильтр не задан" (значение по умолчанию).
_ParentAliasArg = Union[str, None, _ParentAliasNotSpecified]


class MappingStore(abc.ABC):
    """
    Контракт хранилища соответствий real_value <-> alias.

    Важно про идентичность записи: real_value НЕ считается глобально
    уникальным ключом. Одна и та же строка может законно встречаться в
    разных EntityType и в разных ветках иерархии (разные parent_alias).
    Поэтому полная идентичность записи — это тройка
    (real_value, entity_type, parent_alias), а не одно real_value.

    get_by_real_value принимает entity_type и parent_alias как
    НЕОБЯЗАТЕЛЬНЫЕ ФИЛЬТРЫ, но с разной семантикой "не задано":

    - entity_type: None означает "не фильтровать по типу" — у
      MappingEntry entity_type обязателен и никогда не бывает None,
      поэтому значение None однозначно свободно для роли "любой тип".

    - parent_alias: здесь три состояния, а не два:
        1) параметр вообще не передан -> "не фильтровать по родителю"
           (сопоставляются записи с любым parent_alias, включая None);
        2) parent_alias=None передан ЯВНО -> искать запись именно БЕЗ
           родителя (top-level сущность, у которой parent_alias
           действительно None);
        3) parent_alias="B_..." передан явно -> искать запись именно с
           этим родителем.
      Различение (1) и (2) реализовано через приватный sentinel
      _PARENT_UNSET — деталь реализации, не касающаяся вызывающего кода:
      достаточно либо не передавать parent_alias вовсе, либо передать
      конкретное значение (включая None).

    Если после применения фильтров подходит больше одной записи —
    поднимается AmbiguousMappingError; MappingStore никогда не выбирает
    произвольную запись из нескольких подходящих.

    MappingStore не проверяет, существует ли сущность с alias, указанным
    в parent_alias добавляемой записи: связность иерархии — забота
    отдельного компонента, который появится позже.

    На этом этапе хранилище также не выполняет никакой нормализации
    real_value (trim, регистр, Unicode-форма и т.д.) — строки сравниваются
    и ищутся ровно так, как были переданы.
    """

    @abc.abstractmethod
    def add(self, entry: MappingEntry) -> None:
        """
        Добавляет запись mapping.

        Повторное добавление ТОЙ ЖЕ (по значению) записи — не ошибка,
        состояние хранилища не меняется (идемпотентность).

        :raises MappingConflictError: если alias записи уже связан с
            другой записью, либо полная identity записи
            (real_value, entity_type, parent_alias) уже связана с другим
            alias.
        :raises TypeError: если entry не является MappingEntry.
        """

    @abc.abstractmethod
    def get_by_alias(self, alias: str) -> Optional[MappingEntry]:
        """Возвращает запись по alias, либо None, если такого alias нет."""

    @abc.abstractmethod
    def get_by_real_value(
        self,
        real_value: str,
        entity_type: Optional[EntityType] = None,
        parent_alias: _ParentAliasArg = _PARENT_UNSET,
    ) -> Optional[MappingEntry]:
        """
        Ищет запись по real_value с необязательными фильтрами entity_type
        и parent_alias (см. подробное описание трёх состояний parent_alias
        в docstring класса).

        :returns: единственную подходящую запись, либо None, если ни одна
            запись не подходит.
        :raises AmbiguousMappingError: если подходит более одной записи.
        """

    @abc.abstractmethod
    def contains_alias(self, alias: str) -> bool:
        """Проверяет, существует ли запись с данным alias."""

    @abc.abstractmethod
    def all_aliases(self) -> set[str]:
        """
        Возвращает snapshot всех alias, известных хранилищу.

        Возвращённый набор — независимая копия: его изменение не влияет
        на внутреннее состояние хранилища.
        """

    @abc.abstractmethod
    def entries(self) -> tuple[MappingEntry, ...]:
        """
        Возвращает snapshot всех записей хранилища.

        MappingEntry неизменяем (frozen), а сам возвращаемый tuple —
        независимая копия внутренней коллекции.
        """

    @abc.abstractmethod
    def clear(self) -> None:
        """Полностью очищает хранилище (все внутренние индексы)."""
