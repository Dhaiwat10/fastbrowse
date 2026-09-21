"""Keep a run on its backup after the chosen Jev provider exhausts its retries."""

from collections.abc import Mapping

from pydantic import JsonValue

from fastbrowse.clients.validation import estimated_cost
from fastbrowse.jev import Evaluation, JevClient, JevRetriesExhausted, Question
from fastbrowse.models import CostBasis


class FailoverJevClient:
    def __init__(self, primary: JevClient, backup: JevClient) -> None:
        self._active = primary
        self._backup = backup

    async def evaluate(self, state: JsonValue, questions: Mapping[str, Question]) -> Evaluation:
        client = self._active
        try:
            return await client.evaluate(state, questions)
        except JevRetriesExhausted as error:
            if client is self._backup:
                raise
            # Calls already in flight may fail together; assigning the same backup without awaiting cannot flap.
            self._active = self._backup
            try:
                result = await self._backup.evaluate(state, questions)
            except JevRetriesExhausted as backup_error:
                raise JevRetriesExhausted(
                    str(backup_error),
                    status_code=backup_error.status_code,
                    seconds=error.seconds + backup_error.seconds,
                    unaccounted_requests=error.unaccounted_requests + backup_error.unaccounted_requests,
                ) from backup_error

            cost = result.cost
            if error.unaccounted_requests:
                # Only unanswered requests may be billed. Estimate their inputs without multiplying backup hedges.
                discarded = estimated_cost(result.input_tokens * error.unaccounted_requests)
                cost = cost.model_copy(
                    update={
                        "basis": CostBasis.UNKNOWN if cost.dollars is None else CostBasis.ESTIMATED,
                        "dollars": None if cost.dollars is None else cost.dollars + (discarded.dollars or 0),
                        "input_tokens": cost.input_tokens + discarded.input_tokens,
                    }
                )
            return result.model_copy(
                update={"cost": cost.model_copy(update={"seconds": error.seconds + (cost.seconds or 0)})}
            )
