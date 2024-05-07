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
const github = __importStar(require("@actions/github"));
const core = __importStar(require("@actions/core"));
const issue = __importStar(require("./issue"));
const team = __importStar(require("./team"));
function run() {
    return __awaiter(this, void 0, void 0, function* () {
        const githubToken = core.getInput("github-token", { required: true });
        const correspondence = core.getInput("correspondence", { required: false });
        let issueNumber = github.context.issue.number;
        let owner = github.context.repo.owner;
        let repo = github.context.repo.repo;
        // parse correspondence to map
        let corrMap = JSON.parse(correspondence);
        if (corrMap.size == 0) {
            return;
        }
        // get teams and corresponding members
        let teams = Array.from(corrMap.keys());
        let teamsMember = yield team.getTeamMemberMap(githubToken, teams);
        // get all assignees for issue
        let assignees = yield issue.listAssignees(githubToken, owner, repo, issueNumber);
        // get all labels for issue
        let labels = yield issue.listLabels(githubToken, owner, repo, issueNumber);
        let added = new Set();
        let deleted = new Set();
        for (const assignee of assignees) {
            let corrTeam = findTeam(teamsMember, assignee);
            if (corrTeam === null) {
                continue;
            }
            // get label and check whether issue already labeled it
            let corrLabel = corrMap.get(corrTeam);
            if (labels.includes(corrLabel)) {
                continue;
            }
            added.add(corrLabel);
        }
        // generate need delete label array
        for (const corrMapElement of corrMap.values()) {
            if (added.has(corrMapElement)) {
                continue;
            }
            if (!labels.includes(corrMapElement)) {
                continue;
            }
            deleted.add(corrMapElement);
        }
        yield issue.addLabels(githubToken, owner, repo, issueNumber, Array.from(added));
        yield issue.removeLabels(githubToken, owner, repo, issueNumber, Array.from(deleted));
    });
}
function findTeam(teamsMap, member) {
    for (const entry of teamsMap.entries()) {
        if (entry[1].includes(member)) {
            return entry[0];
        }
    }
    return null;
}
function main() {
    return __awaiter(this, void 0, void 0, function* () {
        try {
            yield run();
        }
        catch (err) {
            core.setFailed(err);
        }
    });
}
main();
