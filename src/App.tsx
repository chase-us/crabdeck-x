import { useAuth } from "@workos-inc/authkit-react";
import { useMutation, usePaginatedQuery, useQuery } from "convex/react";
import { FormEvent, useEffect, useState } from "react";
import { api } from "../convex/_generated/api";

const WORKOS_CLIENT_ID = import.meta.env.VITE_WORKOS_CLIENT_ID;
const CONVEX_URL = import.meta.env.VITE_CONVEX_URL;

export default function App() {
  return (
    <div className="deck">
      <header className="mast">
        <div className="brand">
          <span className="glyph" aria-hidden="true">
            🦀
          </span>
          <div>
            <p className="eyebrow">Sovereign orchestration</p>
            <h1>CRABDECK X</h1>
          </div>
        </div>
        <p className="lede">
          Unified control layer for multi-agent intelligence. Live Convex
          backend, WorkOS identity, real-time task queue.
        </p>
      </header>

      <main className="grid">
        <StatusPanel />
        <TaskConsole />
      </main>
    </div>
  );
}

function StatusPanel() {
  if (!CONVEX_URL) {
    return <StatusPanelView health={undefined} />;
  }

  return <LiveStatusPanel />;
}

function LiveStatusPanel() {
  const health = useQuery(api.health.status);
  return <StatusPanelView health={health} />;
}

function StatusPanelView({
  health,
}: {
  health:
    | {
        ok: true;
        product: string;
        tables: string[];
      }
    | undefined
    | null;
}) {
  return (
    <section className="panel rail">
      <h2>Deck status</h2>
      <ul className="status-list">
        <StatusRow
          label="Convex"
          ok={Boolean(CONVEX_URL) && health?.ok === true}
          detail={
            CONVEX_URL
              ? health?.ok
                ? "Reactive backend online"
                : "Connecting…"
              : "Run npx convex dev"
          }
        />
        <StatusRow
          label="Schema"
          ok={health?.ok === true}
          detail={health ? health.tables.join(" · ") : "users · tasks"}
        />
        <StatusRow
          label="WorkOS"
          ok={Boolean(WORKOS_CLIENT_ID)}
          detail={
            WORKOS_CLIENT_ID
              ? "AuthKit client configured"
              : "Add VITE_WORKOS_CLIENT_ID"
          }
        />
      </ul>
      <ol className="runbook">
        <li>
          <code>npx convex dev</code> — development backend only, not production
          deploy
        </li>
        <li>
          Copy <code>VITE_WORKOS_CLIENT_ID</code> into <code>.env.local</code>
        </li>
        <li>
          Sign in, then the task queue becomes a live subscription
        </li>
      </ol>
    </section>
  );
}

function StatusRow({
  label,
  ok,
  detail,
}: {
  label: string;
  ok: boolean;
  detail: string;
}) {
  return (
    <li>
      <span className={ok ? "led on" : "led"} aria-hidden="true" />
      <div>
        <strong>{label}</strong>
        <p>{detail}</p>
      </div>
    </li>
  );
}

function TaskConsole() {
  if (!CONVEX_URL) {
    return (
      <section className="panel">
        <h2>Mission queue</h2>
        <p className="empty">
          Convex is not configured. Create <code>.env.local</code> from{" "}
          <code>.env.example</code> and run <code>npx convex dev</code>.
        </p>
      </section>
    );
  }

  if (!WORKOS_CLIENT_ID) {
    return (
      <section className="panel">
        <h2>Mission queue</h2>
        <p className="empty">
          Tasks are private to the signed-in user. Add your WorkOS AuthKit
          client ID as <code>VITE_WORKOS_CLIENT_ID</code>, then restart the
          Vite app.
        </p>
      </section>
    );
  }

  return <AuthenticatedQueue />;
}

function AuthenticatedQueue() {
  const { user, isLoading, signIn, signOut } = useAuth();
  const storeUser = useMutation(api.users.store);
  const me = useQuery(api.users.me);

  useEffect(() => {
    if (user) {
      void storeUser();
    }
  }, [storeUser, user]);

  if (isLoading) {
    return (
      <section className="panel">
        <h2>Mission queue</h2>
        <p className="empty">Checking identity…</p>
      </section>
    );
  }

  if (!user) {
    return (
      <section className="panel">
        <h2>Mission queue</h2>
        <p className="empty">
          Sign in with WorkOS to create, complete, and delete your own tasks.
          Every query is scoped to your user row.
        </p>
        <button type="button" className="primary" onClick={() => void signIn()}>
          Sign in
        </button>
      </section>
    );
  }

  return (
    <section className="panel">
      <div className="panel-head">
        <div>
          <h2>Mission queue</h2>
          <p className="operator">
            Operator {me?.name ?? user.firstName ?? user.email ?? "signed in"}
          </p>
        </div>
        <button type="button" className="ghost" onClick={() => void signOut()}>
          Sign out
        </button>
      </div>
      {me ? <TaskList /> : <p className="empty">Provisioning operator…</p>}
    </section>
  );
}

function TaskList() {
  const { results, status, loadMore } = usePaginatedQuery(
    api.tasks.list,
    {},
    { initialNumItems: 20 },
  );
  const create = useMutation(api.tasks.create);
  const update = useMutation(api.tasks.update);
  const remove = useMutation(api.tasks.remove);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function onCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const title = draft.trim();
    if (!title) {
      return;
    }
    try {
      setError(null);
      await create({ title });
      setDraft("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Failed to create task");
    }
  }

  return (
    <>
      <form className="composer" onSubmit={(event) => void onCreate(event)}>
        <label htmlFor="task-title">New task</label>
        <div className="composer-row">
          <input
            id="task-title"
            name="title"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder="Stand up the next agent lane"
            required
            maxLength={200}
          />
          <button type="submit" className="primary">
            Queue
          </button>
        </div>
      </form>
      {error ? <p className="error">{error}</p> : null}
      {results.length === 0 ? (
        <p className="empty">No missions yet. Queue the first one.</p>
      ) : (
        <ul className="tasks">
          {results.map((task) => (
            <li key={task._id} className={task.completed ? "done" : undefined}>
              <label>
                <input
                  type="checkbox"
                  checked={task.completed}
                  onChange={(event) =>
                    void update({
                      taskId: task._id,
                      completed: event.target.checked,
                    })
                  }
                />
                <span>{task.title}</span>
              </label>
              <button
                type="button"
                className="ghost danger"
                onClick={() => void remove({ taskId: task._id })}
              >
                Delete
              </button>
            </li>
          ))}
        </ul>
      )}
      {status === "CanLoadMore" ? (
        <button type="button" className="ghost" onClick={() => loadMore(20)}>
          Load more
        </button>
      ) : null}
    </>
  );
}
