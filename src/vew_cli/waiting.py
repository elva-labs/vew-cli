"""Polling of asynchronous operations until the resource reaches a terminal status."""

import time
from collections.abc import Callable
from dataclasses import dataclass

import click


class WaitFailed(Exception):
    exit_code = 3


class WaitTimeout(Exception):
    exit_code = 4


class CreateUnresolved(Exception):
    """The API accepted a create but the new resource could not be told apart from the others."""

    exit_code = 5


@dataclass(frozen=True)
class WaitSpec:
    success: frozenset[str]
    failure: frozenset[str]

    @classmethod
    def of(cls, success: set[str], failure: set[str]) -> "WaitSpec":
        return cls(frozenset(success), frozenset(failure))


def wait_for(
    fetch: Callable[[], dict],
    status_of: Callable[[dict], str | None],
    spec: WaitSpec,
    *,
    timeout: float,
    interval: float,
    label: str,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    deadline = clock() + timeout
    last_status = None
    while True:
        response = fetch()
        status = status_of(response)
        if status != last_status:
            click.echo(f"{label}: {status}", err=True)
            last_status = status
        if status in spec.success:
            return response
        if status in spec.failure:
            raise WaitFailed(f"{label} finished with status {status}")
        if clock() + interval > deadline:
            raise WaitTimeout(f"Timed out after {timeout:.0f}s waiting for {label}; last status {status}")
        sleep(interval)


def find_created(
    list_items: Callable[[], list[dict]],
    id_key: str,
    before: set[str],
    matches: Callable[[dict], bool],
    *,
    label: str,
    hint: str,
    timeout: float = 120,
    interval: float = 3,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    """Finds the resource a create added: the user APIs accept creates without returning the new resource's ID.

    `before` holds the IDs listed just before the create, and `matches` checks the fields the request set, so
    the result is only ambiguous when an identical create ran at the same time.
    """
    deadline = clock() + timeout
    while True:
        candidates = [item for item in list_items() if item.get(id_key) not in before and matches(item)]
        if len(candidates) == 1:
            return candidates[0]
        if candidates:
            ids = ", ".join(str(item.get(id_key)) for item in candidates)
            raise CreateUnresolved(f"The {label} was created, but {len(candidates)} new ones match it ({ids}). {hint}")
        if clock() + interval > deadline:
            raise CreateUnresolved(f"The {label} was accepted but did not appear within {timeout:.0f}s. {hint}")
        sleep(interval)
