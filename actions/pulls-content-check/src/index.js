import core from '@actions/core';
import github from '@actions/github';
import chalk from 'chalk';
import { fromMarkdown } from 'mdast-util-from-markdown';
import { toString } from 'mdast-util-to-string'

const thisRepo = core.getInput("this_repo",{required: false})
if (thisRepo != `${github.context.repo.owner}/${github.context.repo.repo}`) {
    core.debug(`${github.context.owner}/${github.context.repo}`)
    core.info(`This repo is not required repo ${thisRepo}, so return true`)
    core.setOutput("pull_valid", `true`)
    process.exit(0); 
}

const githubToken = core.getInput("github_token", { required: true });
const oc = github.getOctokit(githubToken);

async function main() {
    const titleIssue = core.getInput("title_for_find_issue");
    const titleContent = core.getInput("title_for_find_content");

    if (github.context.payload.pull_request === undefined) {
        core.setFailed("the event which trigger this workflow must is pull_request or pull_request_target")
        return 
    }

    const {data: pull} = await oc.rest.pulls.get({
        ...github.context.repo,
        pull_number: github.context.payload.pull_request.number
    })
    let pullContent = pull.body;
    if (pullContent === undefined || pullContent.length == 0) {
        core.setFailed(`this request is not pull_request or the body of this pull_request is empty`);
    }

    const urlReplace = /\[.*\]\((http.*)\)/igm

    pullContent = pullContent.replace(urlReplace,"$1")

    core.debug("pull content is :" + pullContent)

    let issueIsValid = true
    let contentIsValid = true

    const tree = fromMarkdown(pullContent)

    for (let i = 0; i < tree.children.length; i++) {
        const content = toString(tree.children[i]);
        let inputContent = "";
        switch (content) {
            case titleIssue:
                // i+1 to skip title
                i, inputContent = drumpToNextHeading(tree, i + 1)
                issueIsValid = await checkIssueValid(inputContent);
                continue;
            case titleContent:
                i, inputContent = drumpToNextHeading(tree, i + 1)
                contentIsValid = checkContentValid(inputContent);
                continue;
            default:
                continue;
        }
    }

    core.setOutput("pull_valid", `${issueIsValid && contentIsValid}`)
    if (! (issueIsValid && contentIsValid)) {
        core.setFailed("please add releated issue number(url) under heading `" + chalk.greenBright(titleIssue) + "` and describe the motive of this PR under heading `" + chalk.greenBright(titleContent) + "`")
    }
}

async function checkIssueValid(issueContent) {
    core.debug("issue content is: " + chalk.greenBright(issueContent))

    // check short issue in other repo, such as matrixorigin/MO-Cloud#1
    let regOtherShort = /([a-zA-Z0-9\_\.\-]+)\/([a-zA-Z0-9\_\.\-]+)#([0-9]+)/igm
    let resultOtherShortRepo = issueContent.matchAll(regOtherShort)
    let otherShortIssue = resultOtherShortRepo.next();
    let haveShortNext = !otherShortIssue.done;
    while (haveShortNext) {
        core.info(`start check other repo short issue ` + chalk.greenBright(otherShortIssue.value[0]))
        const { data: data, status: status } = await oc.rest.issues.get({
            owner: otherShortIssue.value[1],
            repo: otherShortIssue.value[2],
            issue_number: otherShortIssue.value[3]
        });
        if (status == 200 && data.pull_request === undefined) {
            core.info("issue " + chalk.red(`${otherShortIssue.value[3]}`) + " in other repo " + chalk.greenBright(`${ otherShortIssue.value[1]}/${ otherShortIssue.value[2]}`) + " is valid, so return true")
            return true
        }
        otherShortIssue = resultOtherShortRepo.next();
        haveShortNext = !otherShortIssue.done;
    }

    // check issue in this repo, such as #1
    let regSlef = /#[0-9]+/igm
    let result = issueContent.match(regSlef)
    if (result !== null && result.length != 0) {
        // check weather issue number is valid
        for (let i = 0; i < result.length; i++) {
            // remove prefix #
            const issue = result[i].substring(1);
            core.info("start check issue " + chalk.red(`${issue}`) + " in this repo " + chalk.greenBright(`${github.context.repo.owner}/${github.context.repo.repo}`));
            const { data: data, status: status } = await oc.rest.issues.get({
                ...github.context.repo,
                issue_number: issue
            });
            if (status == 200 && data.pull_request === undefined) {
                core.info("issue " + chalk.red(`${issue}`) + " in this repo " + chalk.greenBright(`${github.context.repo.owner}/${github.context.repo.repo}`) + " is valid, so return true")
                return true
            }

        }
    }

    // check issue in other repo, such as https://github.com/matrixorigin/matrixone/issues/1
    const regOther = /https:\/\/github.com\/([a-zA-Z0-9\-_\.]+)\/([a-zA-Z0-9\-_\.]+)\/issues\/(\d+)/igm;
    let resultOtherRepo = issueContent.matchAll(regOther)

    let otherIssue = resultOtherRepo.next();
    let haveNext = !otherIssue.done;
    while (haveNext) {
        core.info(`start check other repo long issue ` + chalk.greenBright(otherIssue.value[0]))
        const { data: data, status: status } = await oc.rest.issues.get({
            owner: otherIssue.value[1],
            repo: otherIssue.value[2],
            issue_number: otherIssue.value[3]
        });
        if (status == 200 && data.pull_request === undefined) {
            core.info("issue " + chalk.red(`${otherIssue.value[3]}`) + " in other repo " + chalk.greenBright(`${ otherIssue.value[1]}/${ otherIssue.value[2]}`) + " is valid, so return true")
            return true
        }
        otherIssue = resultOtherRepo.next();
        haveNext = !otherIssue.done;
    }
    core.info(chalk.red("there is no valid issue, so return false"));
    return false
}

function drumpToNextHeading(tree, ind) {
    let content = toString(tree.children[ind]);
    while (ind + 1 < tree.children.length && tree.children[ind + 1].type != "heading") {
        content += " " + toString(tree.children[++ind]);
    }
    return ind, content;
}

function checkContentValid(messageContent) {
    core.info("pull message is: " + chalk.greenBright(messageContent));
    messageContent = messageContent.replace("debug", "").replace("fix", "").replace(/<img.*>/igm,"img").replace(/[!"#$%&'()*+,-./:;<=>?@\[\]\^_`{|}~ \\]/igm, "")
    core.info("after replace: " + chalk.red(messageContent))
    core.info(messageContent.length)
    if (messageContent.length >= 3) {
        return true
    }
    return false
}

main();
