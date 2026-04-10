from typing import AsyncIterator
import structlog

logger = structlog.get_logger()


class StreamingMixin:
    async def stream_text(
        self, text_iterator: AsyncIterator[str]
    ) -> AsyncIterator[str]:
        """Process streaming text with buffering."""
        buffer = ""
        async for chunk in text_iterator:
            buffer += chunk
            if len(buffer) >= 100 or "\n" in buffer:
                yield buffer
                buffer = ""
        if buffer:
            yield buffer

    def format_stream_output(
        self, chunks: list[str], include_timing: bool = False
    ) -> str:
        """Format streaming chunks into final output."""
        output = "".join(chunks)
        if include_timing:
            return output
        return output
