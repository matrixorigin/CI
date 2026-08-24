export function normalizePullContent(body) {
    return typeof body === "string" ? body : "";
}
