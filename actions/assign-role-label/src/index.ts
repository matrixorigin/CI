import * as github from "@actions/github"
import * as core from "@actions/core"
import * as issue from "./issue"
import * as team from "./team"




async function run() {
    const githubToken = core.getInput("github-token", { required: true })
    const correspondence = core.getInput("correspondence", { required: false })

    let issueNumber = github.context.issue.number
    let owner = github.context.repo.owner
    let repo = github.context.repo.repo

    let corrMap = new Map<string,string>()

    // parse correspondence to map
    let corrRecord = JSON.parse(correspondence) as Record<string, string>
    for (const entry of Object.entries(corrRecord)) {
        corrMap.set(entry[0],entry[1])
    }

    if (corrMap.size == 0) {
        return
    }

    // get teams and corresponding members
    let teams = Array.from(corrMap.keys())
    let teamsMember = await team.getTeamMemberMap(githubToken,teams)

    // get all assignees for issue
    let assignees = await issue.listAssignees(githubToken,owner,repo,issueNumber)

    // get all labels for issue
    let labels = await issue.listLabels(githubToken,owner,repo,issueNumber)

    let added = new Set<string>()
    let deleted =  new Set<string>()

    for (const assignee of assignees) {
        let corrTeam = findTeam(teamsMember,assignee)
        if (corrTeam === null) {
            continue
        }
        // get label and check whether issue already labeled it
        let corrLabel = corrMap.get(corrTeam)
        if (labels.includes(corrLabel as string)) {
            continue
        }
        added.add(corrLabel as string)
    }

    // generate need delete label array
    for (const corrMapElement of corrMap.values()) {
        if (added.has(corrMapElement)) {
            continue
        }
        if (!labels.includes(corrMapElement)) {
            continue
        }
        deleted.add(corrMapElement)
    }

    await issue.addLabels(githubToken,owner,repo,issueNumber,Array.from(added))
    await issue.removeLabels(githubToken,owner,repo,issueNumber,Array.from(deleted))

}

function findTeam(teamsMap: Map<string, string[]>,member: string) {
    for (const entry of teamsMap.entries()) {
        if (entry[1].includes(member)) {
            return entry[0]
        }
    }
    return null
}

async function main() {
    try {
        await run();
    }catch (err) {
        core.setFailed(err as Error)
    }
}

main();