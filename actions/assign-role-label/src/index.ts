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
    core.info("get all assignees success: "+ assignees.join(","))

    // get all labels for issue
    let labels = await issue.listLabels(githubToken,owner,repo,issueNumber)
    core.info("get issue labels success: "+ labels.join(","))

    let added = new Set<string>()
    let deleted =  new Set<string>()

    core.info("start check assignees team and corresponding label")
    for (const assignee of assignees) {
        let corrTeam = findTeam(teamsMember,assignee)
        if (corrTeam === null) {
            core.info(`the assignee ${assignee} does not have corresponding required team`)
            continue
        }
        core.info(`the assignee ${assignee} correspond to team ${corrTeam}`)
        // get label and check whether issue already labeled it
        let corrLabel = corrMap.get(corrTeam)
        if (labels.includes(corrLabel as string)) {
            core.info(`assignee ${assignee}, team ${corrTeam} corresponding label ${corrLabel as string} already labeled`)
            continue
        }
        core.info(`assignee ${assignee}, team ${corrTeam} need add label ${corrLabel as string}`)
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

    core.info("this action will add labels: " + Array.from(added).join(","))
    await issue.addLabels(githubToken,owner,repo,issueNumber,Array.from(added))

    core.info("this action will remove labels: " + Array.from(deleted).join(","))
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