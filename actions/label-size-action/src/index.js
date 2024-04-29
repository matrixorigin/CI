const core = require('@actions/core');
const github = require('@actions/github');

const github_token = core.getInput("size_token", { required: true });
const ignoreStr = core.getInput("ignore");
const sizes = core.getInput("sizes");

const defaultSzie = {
    "XS": 0,
    "S": 10,
    "M": 100,
    "L": 500,
    "XL": 1000,
    "XXL": 2000,
}


async function run() {

    try {
        //get a octokit client
        const octokit = github.getOctokit(github_token, { userAgent: "guguducken/label-size-action" });

        //get github context
        const context = github.context;

        //get pull request num
        const num = context.payload?.pull_request?.number;

        if (num == undefined) {
            core.info("This job is not triggered by PR");
            return
        }

        core.info("----------------------------Labeler Start---------------------------");

        //get repo and owner
        const repo_name = context.repo.repo;
        const repo_owner = context.repo.owner;

        core.info(`The target repository name is: ` + repo_name);
        core.info(`The owner of this repository is: ` + repo_owner);
        core.info(`The PR number of this job is: ` + num);

        //get ignore file RegExp
        let ignore_re = getIgnoreRe(ignoreStr);

        //get files
        const { data: files } = await octokit.rest.pulls.listFiles(
            {
                ...context.repo,
                pull_number: num,
            }
        );

        //get labels of this PR 
        const { data: pr } = await octokit.rest.pulls.get(
            {
                ...context.repo,
                pull_number: num,
            }
        );
        let { labels } = pr;

        //get the size of file changes, additions and deletions
        const { changedSize, additions, deletions } = getChangeSize(files, ignore_re);
        core.info("----------------------------Changed Sizes---------------------------");
        core.info("The additions of this PR: " + additions);
        core.info("The deletions of this PR: " + deletions);
        core.info("The changedSize of this PR: " + changedSize);


        const labelSize = JSON.parse(sizes) || defaultSzie;

        //get the label of size
        const label = getLabel(changedSize, labelSize);

        //get the label which need to add and remove
        let { add, move } = getAddAndMove(labels, label);

        //check label status
        if (add.length == 0 && move.length == 0) {
            core.info("----------------------------Labeler Names---------------------------");
            core.info("No New Label need to add or move!");
            core.info("----------------------------Labeler Finish---------------------------");
            return;
        }

        core.info("----------------------------Labeler Names---------------------------");
        core.info("The label to be add is: " + add);
        core.info("The label to be remove is: " + move);

        //move label
        if (move.length != 0) {
            for (const label of move) {
                await octokit.rest.issues.removeLabel(
                    {
                        ...context.repo,
                        issue_number: num,
                        name: label
                    }
                )
            }
        }

        //add label
        if (add.length != 0) {
            await octokit.rest.issues.addLabels(
                {
                    ...context.repo,
                    issue_number: num,
                    labels: add
                }
            )
        }
        core.info("----------------------------Labeler Finish---------------------------");
    } catch (err) {
        core.setFailed(err.message);
    }
}

function getAddAndMove(labels, newLabel) {
    let add = [newLabel];
    let move = [];
    for (let i = 0; i < labels.length; i++) {
        const { name } = labels[i];
        if (name.startsWith("size/")) {
            if (name.toUpperCase() == newLabel.toUpperCase()) {
                add.pop();
            } else {
                move.push(name);
            }
        }
    }
    return { add, move }
}

function reParse(str) {
    let ans = "";
    for (let index = 0; index < str.length; index++) {
        const e = str[index];
        if (e == "/" || e == "{" || e == "}" || e == "[" || e == "]" ||
            e == "(" || e == ")" || e == "^" || e == "$" || e == "+" ||
            e == "\\" || e == "." || e == "*" || e == "|" || e == "?") {
            ans += "\\";
        }
        ans += e;
    }
    return ans
}

function getIgnoreRe(ignoreStr) {
    if (ignoreStr === "") {
        return undefined;
    }
    let set_re = new Set();
    let t = "";
    for (let i = 0; i < ignoreStr.length; i++) {
        const e = ignoreStr[i];
        if (e == ",") {
            if (t == "") {
                continue;
            }
            set_re.add(reParse(t));
            t = "";
        } else {
            t += e;
        }
    }
    if (t != "") {
        set_re.add(reParse(t));
    }
    if (set_re.size == 0) {
        return undefined
    }
    let ans_re = new Array();
    for (const re of set_re) {
        ans_re.push(new RegExp(re, "igm"));
    }
    return ans_re
}

function getChangeSize(files, ignore) {
    let changedSize = 0;
    let additions = 0;
    let deletions = 0;
    if (ignore == undefined) {
        for (const file of files) {
            changedSize += file.changes;
            additions += file.additions;
            deletions += file.deletions;
        }
    } else {
        core.info("---------------------------- Files Ignore ---------------------------");
        for (const file of files) {
            let flag = false;
            for (let re of ignore) {
                re.lastIndex = 0;
                if (re.test(file.filename) && re.lastIndex == file.filename.length) {
                    flag = true;
                    break;
                }
            }
            if (flag) {
                core.info("The ignore file is: " + file.filename);
            } else {
                changedSize += file.changes;
                additions += file.additions;
                deletions += file.deletions;
            }
        }
    }
    return { changedSize, additions, deletions };
}

function getLabel(size, labelSize) {
    let label = "";
    for (const tag of Object.keys(labelSize).sort((a, b) => { return labelSize[a] - labelSize[b] })) {
        if (size >= labelSize[tag]) {
            label = tag;
        }
    }
    return "size/" + label.toUpperCase();
}

run();