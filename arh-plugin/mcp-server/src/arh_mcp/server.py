import os

from dotenv import load_dotenv
from fastmcp import FastMCP

_existing_api_key = os.environ.get("ARH_API_KEY")
load_dotenv(dotenv_path=os.path.join(os.getcwd(), ".env"))
if _existing_api_key is None:
    os.environ.pop("ARH_API_KEY", None)

mcp = FastMCP("AI Researcher Hub")

# Register all tool modules
from arh_mcp.tools import agents, research, communication, tracing, workflow

agents.register(mcp)
research.register(mcp)
communication.register(mcp)
tracing.register(mcp)
workflow.register(mcp)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
