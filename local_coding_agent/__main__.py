import asyncio
import argparse
from typing import TYPE_CHECKING

from local_coding_agent import create_agent, configure_logging
from observability.logging import AgentLogger

if TYPE_CHECKING:
    from agent.orchestrator import AgentOrchestrator


async def interactive_mode(agent: "AgentOrchestrator", session_id: str = None, stream: bool = False):
    print("Local Coding Agent - Interactive Mode")
    print("Type 'exit' to quit, 'history' to see conversation, 'sessions' to list all sessions")
    print("-" * 50)

    if session_id:
        print(f"Resuming session: {session_id}")
    else:
        session_id = f"interactive_{asyncio.get_event_loop().time():.0f}"
        agent.session_memory.create_session(session_id, agent.workspace_path)

    while True:
        try:
            task = input("\n> ")
            if task.lower() in ("exit", "quit"):
                break
            if task.lower() == "history":
                history = agent.get_session_history(session_id)
                for msg in history:
                    role = msg["role"].capitalize()
                    content = msg["content"]
                    print(f"\n[{role}]: {content[:200]}...")
                continue
            if task.lower() == "sessions":
                sessions = agent.list_sessions(limit=10)
                print("\nRecent sessions:")
                for s in sessions:
                    print(f"  {s['session_id']} - {s.get('message_count', 0)} messages - {s.get('status', 'unknown')}")
                continue
            if task.lower().startswith("resume "):
                new_session = task[7:].strip()
                session_id = new_session
                print(f"Switched to session: {session_id}")
                continue
            if not task.strip():
                continue

            if stream:
                print("\n")
                full_response = ""
                async for chunk in agent.run_stream(task, session_id):
                    print(chunk["chunk"], end="", flush=True)
                    full_response = chunk["full_response"]
                print("\n")
                agent.session_memory.save_message(session_id, "assistant", full_response)
            else:
                print("\n[Agent is thinking...]\n")
                result = await agent.run_task(task, session_id)

                if result["success"]:
                    print("\n--- Response ---\n")
                    res = result["result"]
                    if res.get("response"):
                        print(res["response"])
                    elif res.get("messages"):
                        for msg in res["messages"]:
                            content = msg.get("content", "").strip()
                            if content:
                                print(content)
                    print()
                else:
                    print(f"\nError: {result.get('error')}")

        except KeyboardInterrupt:
            print("\n\nExiting...")
            break
        except Exception as e:
            print(f"Error: {e}")


async def main():
    parser = argparse.ArgumentParser(description="Local Coding Agent")
    parser.add_argument(
        "--workspace",
        default="./workspace",
        help="Workspace directory path",
    )
    parser.add_argument(
        "--config",
        default="config/models.yaml",
        help="Path to models configuration",
    )
    parser.add_argument(
        "--task",
        help="Run a single task and exit",
    )
    parser.add_argument(
        "--session",
        help="Session ID to resume",
    )
    parser.add_argument(
        "--list-sessions",
        action="store_true",
        help="List all sessions",
    )
    parser.add_argument(
        "--no-history",
        action="store_true",
        help="Don't include conversation history in context",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show detailed logging",
    )
    parser.add_argument(
        "--stream",
        action="store_true",
        help="Stream responses in real-time",
    )

    args = parser.parse_args()

    configure_logging(json_format=args.verbose)
    agent = create_agent(args.workspace, args.config)

    if args.list_sessions:
        sessions = agent.list_sessions(limit=20)
        print("\nSessions:")
        print("-" * 60)
        for s in sessions:
            created = s.get("created_at", "unknown")
            messages = s.get("message_count", 0)
            status = s.get("status", "unknown")
            print(f"{s['session_id']:<30} {messages:>5} msgs  {status:<10}  {created}")
        return

    if args.task:
        result = await agent.run_task(
            args.task, 
            session_id=args.session,
            include_history=not args.no_history
        )
        if result["success"]:
            print("\n--- Response ---\n")
            res = result["result"]
            if res.get("response"):
                print(res["response"])
            elif res.get("messages"):
                for msg in res["messages"]:
                    content = msg.get("content", "").strip()
                    if content:
                        print(content)
        else:
            print(f"\nError: {result.get('error')}")
    else:
        await interactive_mode(agent, args.session, stream=args.stream)


if __name__ == "__main__":
    asyncio.run(main())
