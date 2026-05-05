from dotenv import load_dotenv
from fastmcp import FastMCP

load_dotenv()

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
