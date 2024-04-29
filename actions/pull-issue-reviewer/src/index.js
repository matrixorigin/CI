const core = require('@actions/core');
const github = require('@actions/github');
const axios = require('axios');

const token = core.getInput(`token_action`, { required: true });
const name_label = core.getInput(`name_label`, { required: true });
const uri_hook = core.getInput(`uri_notice`, { required: true });
const mentions = core.getInput(`mentions`, { required: true });
const reviewers = core.getInput(`reviewers`, { required: true });

const arr_reviewers = reviewers.split(",");
const mentioned_list = mentions.split(",");
const arr_name_label = name_label.split(",");

const repo = github.context.repo;
const pull_request = github.context.payload.pull_request;


const oc = github.getOctokit(token);

async function run() {
    if (pull_request === undefined) {
        core.info(`This workflow is not triag by pull request, check again`);
        return;
    }
    let number = pull_request.number;

    core.info(`Start to fetch the date of PR ${number} >>>>>>`);
    let pr = await getPR(repo,number);

    core.info(`Start to check pull ${pr.number}, title: ${pr.title} >>>>>>`);

    if (pr.draft) {
        core.info(`This pr is draft... skip`);
        return;
    }
    if (pr.body === null) {
        core.info(`There is no body in is pr ${pr.number}..... skip`);
        return;
    }
    let num_issues = getReleatedIssueNumber(pr.body);

    if (num_issues.length != 0) {
        core.info(`Get releated issues total: ${num_issues.length}, is ${num_issues}`);
    } else {
        core.info(`Get releated issues total: ${num_issues.length}, so skip this pull`);
        return;
    }
    let mess = `This PR[${pr.base.repo.name}/${pr.number}](${pr.html_url}) needs to be well documented.`
    let state_total = false

    for (let num of num_issues) {
        let {data:issue} = await getIssueDetails(repo,num);
        if (issue.pull_request !== undefined) {
            core.info(`This is a pr -- ${num}... skip`);
            continue;
        }
        let state_self = false;
        let state_parent = false;

        //check itself
        let flag = false
        for (let label of issue.labels) {
            for (let i = 0; i < arr_name_label.length; i++) {
                if (label.name == arr_name_label[i]) {
                    state_self = true
                    flag = true
                    break
                }
            }
            if (flag) {
                break
            }
        }

        //check parents
        let parents = new Set();
        let isSub = isSubtask(issue.title)
        if (isSub) {
            core.info(`This issue ${issue.number} is a subtask, start to check parents issue`)
            let issues = await getParentIssue(issue.body)
            for (let issue_t of issues) {
                core.info(`Start to check parents issue ${issue_t.number}`);
                let flag = false
                for (let label of issue_t.labels) {
                    for (let i = 0; i < arr_name_label.length; i++) {
                        let name = arr_name_label[i];
                        if (label.name == name) {
                            parents.add(issue_t)
                            flag = true
                            state_parent = true
                            break
                        }
                    }
                    if (flag) {
                        break
                    }
                }
                if (flag) {
                    core.info(`This parent issue ${issue_t.number} meet the conditions, add it to message`);
                } else {
                    core.info(`This parent issue ${issue_t.number} does not meet the conditions`);
                    
                }
            }
        }

        //gen message
        if (state_self || state_parent) {
            state_total = true
            mess += ` The associated issue is: [${issue.repository_url.split(`/`).slice(-1)}/${issue.number}](${issue.html_url})`
            if (state_parent) {
                mess += ` and the reletaed parent issue is: `
                for (let parent of parents) {
                    mess += `[${parent.repository_url.split(`/`).slice(-1)}/${parent.number}](${parent.html_url}),`
                }
                mess = mess.substring(0, mess.length - 1) + `;`
            }

        }
    }
    //send message and add reviewers
    if (state_total) {
        let sum = 0
        while (await addReviewers(repo, pr.number, await reviewersHasCheck(repo,pr.number)) == false) {
            sum++
            if (sum == 10) {
                core.info(`Try add reviewers for pull ${pr.number} and issue ${num} ten times... skip`);
                break;
            }
        }
        core.info(`add reviewer finished`)


        //企业微信通知
        sum = 0;
        if (mess[mess.length - 1] == ';') {
            mess = mess.substring(0, mess.length - 1)
        }

        while (await notice_WeCom(`markdown`, mess) != 200) {
            sum++;
            if (sum == 10) {
                core.info(`Try to notice by WeCom for pull ${pr.number} and issue ${num} ten times... skip`);
                break;
            }
        }
    }else {
        core.info(`There is no set label for the corresponding issue`);
    }
}

async function getPR({ owner, repo },number) {
    let { data: pr } = await oc.rest.pulls.get({
        owner: owner,
        repo: repo,
        state: `open`,
        pull_number: number
    })
    return pr
}

async function notice_WeCom(type, message) {
    let notice_payload = {};
    switch (type) {
        case `text`:
            notice_payload = {
                msgtype: `text`,
                text: {
                    content: message,
                    mentioned_list: mentioned_list
                },
            };
            break;
        case `markdown`:
            for (let i = 0; i < mentioned_list.length; i++) {
                const man = mentioned_list[i];
                message += `<@${man}>`
            }
            core.info(`Notice message: ${message}`);
            notice_payload = {
                msgtype: `markdown`,
                markdown: {
                    content: message
                },
            }
            break;
        default:
            break;
    }
    let resp = await axios.post(uri_hook, JSON.stringify(notice_payload), {
        Headers: {
            'Content-Type': 'application/json'
        }
    });
    return resp.status;
}

//reviewers是一个数组
async function addReviewers({ owner, repo },number, reviewers) {
    if (reviewers.length == 0) {
        return true
    }
    core.info(`Add reviewers ${reviewers} to pull request ${number}`);
    let { status: status } = await oc.rest.pulls.requestReviewers({
        owner: owner,
        repo:repo,
        pull_number: number,
        reviewers: reviewers
    });
    if (status != 201) {
        return false;
    }
    return true;
}

function getReleatedIssueNumber(body) {
    const reg = /#(\d+)/igm
    const result = body.match(reg);
    if (result === null) {
        return [];
    }
    let ans = new Set();
    for (let i = 0; i < result.length; i++) {
        const e = result[i];
        ans.add(e.substring(1))
    }
    return Array.from(ans)
}

async function getParentIssue(body) {
    let issues = new Set()
    const totalReg = /^### Parent Issue(.*)### Detail of Subtask/igms
    let result = totalReg.exec(body)
    if (result === null || result[1].length == 0) {
        return Array.from(issues)
    }
    body = result[1]

    const pubReg = /#(\d+)/img
    result = body.match(pubReg)
    if (result !== null) {
        if (result.length != 0) {
            for (let i = 0; i < result.length; i++) {
                const e = result[i];
                issues.add((await getIssueDetails(repo,e.substring(1))).data)
            }
        }
    }


    const priReg = /https:\/\/github.com\/(.+)\/(.+)\/issues\/(\d+)/igm
    result = body.matchAll(priReg)
    result = Array.from(result)
    if (result === null) {
        return Array.from(issues)
    }
    if (result.length != 0) {
        for (let i = 0; i < result.length; i++) {
            const res = result[i];
            issues.add((await getIssueDetails({owner: res[1],repo: res[2]},res[3])).data)
        }
    }
    return Array.from(issues)
}

function isSubtask(title) {
    return /^\[Subtask\]:/igm.test(title)
}

async function getIssueDetails({ owner, repo },number) {
    let { data: issue, status: status } = await oc.rest.issues.get({
        owner: owner,
        repo:repo,
        issue_number: number
    });
    return { data: issue, status: status };
}

async function reviewersHasCheck({ owner, repo },number) {
    let { data: requested_reviewers, status: status } = await oc.rest.pulls.listRequestedReviewers({
        owner: owner,
        repo: repo,
        pull_number: number
    });

    let all = await getApproveReviewers({ owner, repo },number);
    let arr = new Array();
    for (let i = 0; i < requested_reviewers.users.length; i++) {
        all.add(requested_reviewers.users[i].login);
    }
    let arr_all = Array.from(all);
    for (let i = 0; i < arr_reviewers.length; i++) {
        const reviewer = arr_reviewers[i];
        for (let j = 0; j < arr_all.length; j++) {
            const user = arr_all[j];
            if (user == reviewer) {
                break;
            }
            if (j + 1 == arr_all.length) {
                arr.push(reviewer);
            }
        }
    }
    core.info(`All reviewers is: ` + arr_all);
    return arr
}

async function getApproveReviewers({ owner, repo },number) {
    let { data: reivews } = await oc.rest.pulls.listReviews({
        owner: owner,
        repo:repo,
        pull_number: number
    });

    let se = new Set();
    for (let i = 0; i < reivews.length; i++) {
        const user = reivews[i].user;
        se.add(user.login);
    }
    return se;
}

async function main() {
    try {
        await run();
    } catch (error) {
        core.setFailed(error.message);
    }
}

run();
