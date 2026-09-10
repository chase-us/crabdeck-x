export type LogFields = {
  message: string;
  requestId?: string;
  method?: string;
  path?: string;
  status?: number;
  error?: string;
};

export function logJson(
  level: "log" | "warn" | "error",
  fields: LogFields,
): void {
  const line = JSON.stringify(fields);
  switch (level) {
    case "error":
      console.error(line);
      return;
    case "warn":
      console.warn(line);
      return;
    case "log":
      console.log(line);
      return;
    default: {
      const exhaustive: never = level;
      throw new Error(`unhandled log level: ${String(exhaustive)}`);
    }
  }
}
