import { convexTest } from "convex-test";
import { expect, test } from "vitest";
import { api } from "./_generated/api";
import schema from "./schema";

const modules = import.meta.glob("./**/*.ts");

function createTest() {
  return convexTest(schema, modules);
}

function asUser(
  t: ReturnType<typeof createTest>,
  subject: string,
  name: string,
  email: string,
) {
  return t.withIdentity({
    issuer: "https://auth.example.com",
    subject,
    name,
    email,
  });
}

test("health status is public", async () => {
  const t = createTest();
  const health = await t.query(api.health.status, {});
  expect(health.ok).toBe(true);
  expect(health.tables).toEqual(["users", "tasks"]);
});

test("unauthenticated users cannot create tasks", async () => {
  const t = createTest();
  await expect(t.mutation(api.tasks.create, { title: "Nope" })).rejects.toThrow(
    /Not authenticated/,
  );
});

test("user can create, list, update, and delete their own tasks", async () => {
  const t = createTest();
  const ada = asUser(t, "ada", "Ada Lovelace", "ada@example.com");

  await ada.mutation(api.users.store, {});
  const taskId = await ada.mutation(api.tasks.create, {
    title: "Stand up Convex",
  });

  const page = await ada.query(api.tasks.list, {
    paginationOpts: { numItems: 20, cursor: null },
  });
  expect(page.page).toHaveLength(1);
  expect(page.page[0]?.title).toBe("Stand up Convex");
  expect(page.page[0]?.completed).toBe(false);

  await ada.mutation(api.tasks.update, { taskId, completed: true });
  const updated = await ada.query(api.tasks.get, { taskId });
  expect(updated?.completed).toBe(true);

  const done = await ada.query(api.tasks.listByStatus, {
    completed: true,
    paginationOpts: { numItems: 20, cursor: null },
  });
  expect(done.page).toHaveLength(1);

  await ada.mutation(api.tasks.remove, { taskId });
  const empty = await ada.query(api.tasks.list, {
    paginationOpts: { numItems: 20, cursor: null },
  });
  expect(empty.page).toHaveLength(0);
});

test("users cannot read or mutate another user's task", async () => {
  const t = createTest();
  const ada = asUser(t, "ada", "Ada Lovelace", "ada@example.com");
  const alan = asUser(t, "alan", "Alan Turing", "alan@example.com");

  await ada.mutation(api.users.store, {});
  await alan.mutation(api.users.store, {});
  const taskId = await ada.mutation(api.tasks.create, { title: "Private" });

  await expect(alan.query(api.tasks.get, { taskId })).rejects.toThrow(
    /Unauthorized/,
  );
  await expect(
    alan.mutation(api.tasks.update, { taskId, completed: true }),
  ).rejects.toThrow(/Unauthorized/);
  await expect(alan.mutation(api.tasks.remove, { taskId })).rejects.toThrow(
    /Unauthorized/,
  );
});

test("empty titles are rejected", async () => {
  const t = createTest();
  const ada = asUser(t, "ada", "Ada Lovelace", "ada@example.com");
  await ada.mutation(api.users.store, {});
  await expect(ada.mutation(api.tasks.create, { title: "   " })).rejects.toThrow(
    /empty/,
  );
});
