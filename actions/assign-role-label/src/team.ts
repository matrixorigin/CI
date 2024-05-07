import * as github from "@actions/github";

export async function getTeamMemberMap(token: string,teamSlugs: string[]) {
    const octokit = github.getOctokit(token)
    let organization = github.context.repo.owner
    let m = new Map<string,string[]>

    for (let i = 0; i < teamSlugs.length; i++) {
        let team = teamSlugs[i]
        m.set(team,await listTeamMembers(token,organization,team))
    }
    return m
}

export async function listTeamMembers(token:string,organization: string, slug: string): Promise<string[]> {
    const octokit = github.getOctokit(token)
    let arr = new Array<string>
    let page = 1
    while (true) {
        let {data: members} = await octokit.rest.teams.listMembersInOrg({
            org: organization,
            team_slug: slug,
            page: page,
            per_page: 100
        })
        members.forEach((data ) => {
            arr.push(data.login)
        })
        if (members.length == 100) {
            page ++
            continue
        }
        break
    }
    return arr
}
