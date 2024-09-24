import * as github from "@actions/github"
import * as core from "@actions/core"

interface project {
    id: string
    title: string
}
interface projectItem {
    id: string
    project: project
}

const issueNumber = core.getInput('Issue_ID', {required: false}) as unknown as number;
const token = core.getInput("GITHUB_TOKEN", { required: true });
const repoFullName = core.getInput('GITHUB_REPOSITORY_OWNER', {required: false});
const ocClient = github.getOctokit(token)

const correponding = new Map<string,number>([
    ["default",13],
    ["compute-group-1",33],
    ["compute-group-2",36],
    ["storage-group",35],
    ["matrixonecloud",18],
    ["us-group",13],
    ])
// if assignees' team include any special team, we will add not delete any project
const specialTeams = new Array<string>("qa","pm")
// if assignees include any special user, we will add default project to this issue
const specialUsers = new Array<string>("matrix-meow")


function parseRepoInfo(fullName: string) {
    let owner = github.context.repo.owner;
    let repo = github.context.repo.repo;
    if (fullName.length === 0 || fullName.indexOf("/") === -1) {
        core.warning(`no input or valid repo full name, will use github context info`)
        return {owner, repo}
    }
    let detail = fullName.split("/")
    owner = detail[0]
    repo = detail[1]
    return {owner, repo}
}

function getTargetProjects(teams: Set<string>, corr: Map<string,string>) {
    let result = new Set<string>()
    teams.forEach((team) => {
        let nodeID = corr.get(team)
        if (nodeID === undefined) {
            return
        }
        result.add(nodeID)
    })
    core.debug(`target projects is ${Array.from(result.values())}`)
    return Array.from(result.values())
}

async function getProjectNodeID(organization: string,projectNumber:number) {
    const query = `
        query {
          organization(login: "${organization}") {
            projectV2(number: ${projectNumber}) {
              id
            }
          }
        }
      `;
    interface resp {
        organization: {
            projectV2: {
                id: string
            }
        }
    }
    const result = await ocClient.graphql(query) as resp;
    core.debug(`project number: ${projectNumber} ==> node id: ${result.organization.projectV2.id}`);
    return result.organization.projectV2.id
}

async function getIssueContent(owner: string, repo: string,issueNumber: number) {
    core.debug(`start get content for issue ${issueNumber} in repo ${owner}/${repo}`)
    const {data:result} = await ocClient.rest.issues.get({
        owner: owner,
        repo: repo,
        issue_number: issueNumber,
    });
    return result
}

async function listIssueRelatedProjects(owner: string, repo: string, issueNumber: number) {
    core.debug(`start get projects for issue ${issueNumber} in repo ${owner}/${repo}`);
    const query = `
        query {
          repository(owner:"${owner}", name:"${repo}") {
            issue(number:${issueNumber}) {
              projectItems(first:100,includeArchived:false){
                nodes{
                  ... on ProjectV2Item{
                    id
                    project{
                      id
                      title
                    }
                  }
                }
              }
          }
        }
        }
      `;
    interface resp {
        repository: {
            issue: {
                projectItems: {
                    nodes: projectItem[]
                }
            }
        }
    }
    let projectItems = await ocClient.graphql(query) as resp
    return projectItems.repository.issue.projectItems.nodes

}

async function getTeamForUser(organization: string, users: Array<string>) {
    let result = new Map<string,Set<string>>();
    for (const user of users) {
        let s = new Set<string>();
        if (specialUsers.includes(user)) {
            s.add("default");
        }
        result.set(user,s)
    }

    const {data: teams} = await ocClient.rest.teams.list({
        org: organization,
    });
    if (teams.length === 0) {
        throw new Error(`cannot list teams for organization ${organization}`);
    }
    for (const team of teams) {
        const {data: team_member} = await ocClient.rest.teams.listMembersInOrg({
            org: organization,
            team_slug: team.slug,
            per_page: 100
        });
        if (team_member.length == 0) {
            continue
        }
        users.forEach((user) => {
            const t = team_member.find((member) => { return member.login === user})
            if (t !== undefined) {
                // @ts-ignore
                result.get(user).add(team.slug)
            }
        })

    }
    console.log(result)
    return result
}

async function addProject(projectNodeID: string, issueNodeID: string) {
    const query=`
            mutation{
              addProjectV2ItemById(input:{projectId: \"${projectNodeID}\" contentId: \"${issueNodeID}\" }){
                  item  {
                     id   
                    }
                  }
            }
          `;
    core.debug(`add project ${projectNodeID} for issue ${issueNodeID}`);
    await ocClient.graphql(query)
}

async function removeProject(projectNodeID: string, itemID: string) {
    const query=`
            mutation{
              deleteProjectV2Item(input:{projectId: \"${projectNodeID}\" itemId: \"${itemID}\" }){
                  deletedItemId  
                  }
            }
          `;
    core.debug(`remove item ${itemID} from project ${projectNodeID}`);
    await ocClient.graphql(query)
}

function reverseProjectItems(projectItems: Array<projectItem>) {
    let result = new Map<string,Set<string>>();
    for (const item of projectItems) {
        if (!result.has(item.project.id)) {
            result.set(item.project.id,new Set<string>());
        }
        // @ts-ignore
        result.get(item.project.id).add(item.id)
    }
    return result;
}

async function main() {
    const {owner:owner,repo:repo} = parseRepoInfo(repoFullName)
    const issueContent = await getIssueContent(owner,repo,issueNumber)
    const relatedProjects = await listIssueRelatedProjects(owner,repo,issueContent.number)
    const nowProjects = relatedProjects.map((p) => { return p.project.id })
    const corrProjectNodeIDs = new Map<string, string>();
    for (let project of correponding) {
        corrProjectNodeIDs.set(project[0],await getProjectNodeID(owner, project[1]))
    }

    const assignees = issueContent.assignees?.map((data) => {
        return data.login
    })

    let addedProject = new Array<string>()
    let deletedProjects = new Array<string>()
    if (assignees === undefined) {
        addedProject.push(corrProjectNodeIDs.get("default") as string)
        deletedProjects = nowProjects.filter((p) => { return !addedProject.includes(p) })
    }else {
        const targetTeams = await getTeamForUser(owner,assignees as string[])
        const totalTeams = new Set<string>()
        targetTeams.forEach((item) => { item.forEach((t) => { totalTeams.add(t) }) })
        core.debug(`target total teams is: ${totalTeams}`)
        const targetProjects = getTargetProjects(totalTeams,corrProjectNodeIDs)
        addedProject = targetProjects.filter((p) => { return !nowProjects.includes(p) })
        core.debug(`will added projects is: ${addedProject}`)

        // check special team
        let included = false
        for (let t of totalTeams.keys()) {
            if (specialTeams.includes(t)){
                included = true
                break
            }
        }
        if (!included) {
            deletedProjects = nowProjects.filter((p) => { return !targetProjects.includes(p) })
        }
    }
    addedProject.forEach((p) => { addProject(p,issueContent.node_id) })

    const reversedItems = reverseProjectItems(relatedProjects)
    deletedProjects.forEach((p) => {
        const items = reversedItems.get(p)
        if (items === undefined) {
            return
        }
        items.forEach((i) => { removeProject(p,i) })
    })
}

main()
