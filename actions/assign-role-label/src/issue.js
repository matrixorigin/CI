"use strict";
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || function (mod) {
    if (mod && mod.__esModule) return mod;
    var result = {};
    if (mod != null) for (var k in mod) if (k !== "default" && Object.prototype.hasOwnProperty.call(mod, k)) __createBinding(result, mod, k);
    __setModuleDefault(result, mod);
    return result;
};
var __awaiter = (this && this.__awaiter) || function (thisArg, _arguments, P, generator) {
    function adopt(value) { return value instanceof P ? value : new P(function (resolve) { resolve(value); }); }
    return new (P || (P = Promise))(function (resolve, reject) {
        function fulfilled(value) { try { step(generator.next(value)); } catch (e) { reject(e); } }
        function rejected(value) { try { step(generator["throw"](value)); } catch (e) { reject(e); } }
        function step(result) { result.done ? resolve(result.value) : adopt(result.value).then(fulfilled, rejected); }
        step((generator = generator.apply(thisArg, _arguments || [])).next());
    });
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.removeLabels = exports.addLabels = exports.listAssignees = exports.listLabels = void 0;
const github = __importStar(require("@actions/github"));
const core = __importStar(require("@actions/core"));
function listLabels(token, owner, repo, issueNumber) {
    return __awaiter(this, void 0, void 0, function* () {
        const octokit = github.getOctokit(token);
        let result = new Array();
        let page = 1;
        while (true) {
            let { data: labels } = yield octokit.rest.issues.listLabelsOnIssue({
                owner: owner,
                repo: repo,
                issue_number: issueNumber,
                page: page,
                per_page: 100
            });
            labels.forEach((data) => {
                result.push(data.name);
            });
            if (labels.length == 100) {
                page++;
                continue;
            }
            break;
        }
        return result;
    });
}
exports.listLabels = listLabels;
function listAssignees(token, owner, repo, issueNumber) {
    return __awaiter(this, void 0, void 0, function* () {
        const octokit = github.getOctokit(token);
        let result = new Array();
        let page = 1;
        while (true) {
            let { data: assignees } = yield octokit.rest.issues.listAssignees({
                owner: owner,
                repo: repo,
                issue_number: issueNumber,
                page: page,
                per_page: 100
            });
            assignees.forEach((assignee) => {
                result.push(assignee.login);
            });
            if (assignees.length == 100) {
                page++;
                continue;
            }
            break;
        }
        return result;
    });
}
exports.listAssignees = listAssignees;
function addLabels(token, owner, repo, issueNumber, labels) {
    return __awaiter(this, void 0, void 0, function* () {
        const octokit = github.getOctokit(token);
        octokit.rest.issues.addLabels({
            owner: owner,
            repo: repo,
            issue_number: issueNumber,
            labels: labels
        }).then((resp) => {
            // @ts-ignore
            if (resp.status == 404) {
                core.error("http response status code is " + resp.status + ", this mean Resource not found");
            }
            // @ts-ignore
            if (resp.status == 410) {
                core.error("http response status code is " + resp.status + ", this mean Gone");
            }
            // @ts-ignore
            if (resp.status == 422) {
                core.error("http response status code is " + resp.status + ", this mean Validation failed, or the endpoint has been spammed.");
            }
        });
    });
}
exports.addLabels = addLabels;
function removeLabels(token, owner, repo, issueNumber, labels) {
    return __awaiter(this, void 0, void 0, function* () {
        for (let i = 0; i < labels.length; i++) {
            removeLabel(token, owner, repo, issueNumber, labels[i]).then((resp) => {
                // @ts-ignore
                if (resp.status == 404) {
                    core.error("http response status code is " + resp.status + ", this mean Resource not found");
                }
                // @ts-ignore
                if (resp.status == 410) {
                    core.error("http response status code is " + resp.status + ", this mean Gone");
                }
            });
        }
    });
}
exports.removeLabels = removeLabels;
function removeLabel(token, owner, repo, issueNumber, label) {
    return __awaiter(this, void 0, void 0, function* () {
        const octokit = github.getOctokit(token);
        return yield octokit.rest.issues.removeLabel({
            owner: owner,
            repo: repo,
            issue_number: issueNumber,
            name: label
        });
    });
}
