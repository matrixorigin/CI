import * as github from "@actions/github"
import * as core from "@actions/core"

export async function listLabels(token: string,owner: string,repo: string,issueNumber:number) {
    const octokit = github.getOctokit(token)
    let result = new Array<string>()
    let page = 1
    while (true) {
        let {data: labels} = await octokit.rest.issues.listLabelsOnIssue({
            owner: owner,
            repo: repo,
            issue_number: issueNumber,
            page: page,
            per_page: 100
        })
        if (labels.length == 0) {
            break
        }
        labels.forEach((data) => {
            result.push(data.name)
        })
        if (labels.length == 100) {
            page++
            continue
        }
        break
    }
    return result
}

export async function listAssignees(token: string,owner: string,repo: string,issueNumber:number) {
    const octokit = github.getOctokit(token)
    let result = new Array<string>()
    let page = 1
    while (true) {
        let {data: assignees} =await octokit.rest.issues.listAssignees({
            owner: owner,
            repo: repo,
            issue_number: issueNumber,
            page: page,
            per_page: 100
        })
        if (assignees.length == 0) {
            break
        }
        assignees.forEach((assignee) => {
            result.push(assignee.login)
        })
        if (assignees.length == 100) {
            page++
            continue
        }
        break
    }
    return result
}

export async function addLabels(token: string,owner: string,repo: string,issueNumber:number,labels: string[]) {
    if (labels.length == 0) {
        return
    }
    const octokit = github.getOctokit(token)
    octokit.rest.issues.addLabels({
        owner: owner,
        repo: repo,
        issue_number:issueNumber,
        labels:labels
    }).then(
        (resp) => {
            // @ts-ignore
            if (resp.status == 404) {
                core.error("http response status code is " + resp.status + ", this mean Resource not found")
            }
            // @ts-ignore
            if (resp.status == 410) {
                core.error("http response status code is " + resp.status + ", this mean Gone")
            }
            // @ts-ignore
            if (resp.status == 422) {
                core.error("http response status code is " + resp.status + ", this mean Validation failed, or the endpoint has been spammed.")
            }
        }
    )
}

export async function removeLabels(token: string,owner: string,repo: string,issueNumber:number,labels: string[]) {
    if (labels.length == 0) {
        return
    }
    for (let i = 0; i < labels.length; i++) {
        removeLabel(token,owner,repo,issueNumber,labels[i]).then(
            (resp) => {
                // @ts-ignore
                if (resp.status == 404) {
                    core.error("http response status code is " + resp.status + ", this mean Resource not found")
                }
                // @ts-ignore
                if (resp.status == 410) {
                    core.error("http response status code is " + resp.status + ", this mean Gone")
                }
            }
        )
    }
}

async function removeLabel(token: string,owner: string,repo: string,issueNumber:number,label: string) {
    const octokit = github.getOctokit(token)
    return await octokit.rest.issues.removeLabel({
        owner: owner,
        repo: repo,
        issue_number:issueNumber,
        name:label
    })
}