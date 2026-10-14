"""AfterSale-Agent: fictional local business only."""

import os

# No accidental third-party tracing, even if the parent shell enabled it.
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"
