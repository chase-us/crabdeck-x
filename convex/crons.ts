import { cronJobs } from "convex/server";
import { internal } from "./_generated/api";

const crons = cronJobs();

crons.daily(
  "cleanup-old-tasks",
  { hourUTC: 0, minuteUTC: 0 },
  internal.tasks.cleanupOld,
);

export default crons;
