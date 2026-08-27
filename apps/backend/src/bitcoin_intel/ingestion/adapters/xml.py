from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import Element, ParseError

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from bitcoin_intel.ingestion.adapters.base import RawInputRecord
from bitcoin_intel.ingestion.errors import ErrorCode, IngestionFileError, RecordIssue
from bitcoin_intel.ingestion.models import ARRAY_SOURCE_FIELDS, SOURCE_FIELDS

_ARRAY_ITEM_TAGS = {
    "input_addresses": "address",
    "output_addresses": "address",
    "input_amounts": "amount",
    "output_amounts": "amount",
}


def iter_xml_records(path: Path) -> Iterator[RawInputRecord]:
    stack: list[str] = []
    record_index = 0
    try:
        for event, element in ElementTree.iterparse(path, events=("start", "end")):
            tag = _plain_tag(element)
            if event == "start":
                if not stack and (tag != "records" or element.attrib):
                    raise IngestionFileError("XML root must be an attribute-free <records> element")
                stack.append(tag)
                continue

            if len(stack) == 2:
                if tag != "record":
                    raise IngestionFileError(
                        "XML <records> may contain only direct <record> elements"
                    )
                data, issue = _record_to_mapping(element)
                yield RawInputRecord(record_index, data if issue is None else None, issue)
                record_index += 1
                if (element.tail or "").strip():
                    raise IngestionFileError("XML <records> contains unexpected text")
                element.clear()
            elif len(stack) == 1 and (element.text or "").strip():
                raise IngestionFileError("XML <records> contains unexpected text")
            stack.pop()
    except (ParseError, DefusedXmlException) as error:
        raise IngestionFileError(f"XML input is unsafe or malformed: {error}") from error
    except OSError as error:
        raise IngestionFileError(f"failed to read XML input: {error}") from error


def _record_to_mapping(element: Element) -> tuple[dict[str, Any], RecordIssue | None]:
    if element.attrib:
        return {}, _malformed("XML <record> elements must not have attributes")
    if (element.text or "").strip():
        return {}, _malformed("XML <record> contains unexpected text")
    data: dict[str, Any] = {}
    for child in element:
        field_name = _plain_tag(child)
        if field_name not in SOURCE_FIELDS:
            return {}, _malformed(f"XML record contains unsupported field <{field_name}>")
        if field_name in data:
            return {}, _malformed(f"XML record repeats field <{field_name}>", field_name)
        if field_name in ARRAY_SOURCE_FIELDS:
            values, issue = _parse_array(child, field_name)
            if issue is not None:
                return {}, issue
            data[field_name] = values
        else:
            if len(child) or child.attrib:
                return {}, _malformed(
                    f"XML scalar field <{field_name}> must contain text only", field_name
                )
            data[field_name] = (child.text or "").strip()
        if (child.tail or "").strip():
            return {}, _malformed("XML <record> contains unexpected text")
    return data, None


def _parse_array(element: Element, field_name: str) -> tuple[list[str], RecordIssue | None]:
    expected_item_tag = _ARRAY_ITEM_TAGS[field_name]
    if (element.text or "").strip() or element.attrib:
        return [], RecordIssue(
            ErrorCode.MALFORMED_ARRAY,
            f"XML array <{field_name}> must use repeated <{expected_item_tag}> children",
            field_name,
        )
    values: list[str] = []
    for item in element:
        if _plain_tag(item) != expected_item_tag or len(item) or item.attrib:
            return [], RecordIssue(
                ErrorCode.MALFORMED_ARRAY,
                f"XML array <{field_name}> must use repeated <{expected_item_tag}> children",
                field_name,
            )
        values.append((item.text or "").strip())
        if (item.tail or "").strip():
            return [], RecordIssue(
                ErrorCode.MALFORMED_ARRAY,
                f"XML array <{field_name}> contains unexpected text",
                field_name,
            )
    return values, None


def _plain_tag(element: Element) -> str:
    if not isinstance(element.tag, str) or "}" in element.tag:
        raise IngestionFileError("XML namespaces and non-element nodes are not supported")
    return element.tag


def _malformed(message: str, field_name: str | None = None) -> RecordIssue:
    return RecordIssue(ErrorCode.MALFORMED_SOURCE_RECORD, message, field_name)
