const { Octokit } = require("@octokit/action");
const fetch = require("node-fetch");

const octokit = new Octokit({
  auth: process.env.GITHUB_TOKEN,
});
const githubApiEndpoint = "https://api.github.com/graphql";

const organizationLogin = process.env.GITHUB_REPOSITORY_OWNER;
const token = process.env.GITHUB_TOKEN;
const art = "Bearer "+token;
async function run() {
  try {
    const issueNumber = process.env.Issue_ID;
    // get issue info
    const owner_repo = process.env.GITHUB_REPOSITORY;
    console.log(owner_repo);
    const parts = owner_repo.split('/');
    const issue = await octokit.rest.issues.get({
      owner: parts[0],
      repo: parts[1],         
      issue_number: issueNumber,
    });
    // get assignees list
    const assignees = issue.data.assignees;
    // get issue node_id
    const issue_node_id = issue.data.node_id;
    // graphql header
    const headers = {
        'Authorization': art,
        'Content-Type': 'application/json',
      };
    // Retrieve information about the project to which the current issue belongs.
    var query = `
        query {
          repository(owner:"${organizationLogin}", name:"${parts[1]}") {
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
    var options = {
          method: 'POST',
          headers: headers,
          body: JSON.stringify({ query }),
        };
    const resp_add = await fetch(githubApiEndpoint, options);
    const resp_add_json = await resp_add.json();
    // issue info list
    const issue_list = resp_add_json.data.repository.issue.projectItems.nodes;
    const m1 = new Map();
    if (assignees.length === 0) {
      console.log("The issue has not yet been assigned");
      return;
    }
    const projectMapping = {
        'compute-group-1': 33,
        'compute-group-2': 36,
        'storage-group': 35,
        'mo-cloud-team': 18,
        'mo-qa-team': 99999,
      };
    const issue_item_id = [];
    for(const iss of issue_list){
      issue_item_id.push(iss.project.id);
      m1.set(iss.project.id,iss.id);
    }
    console.log(m1);
    const projectsToAssociate = [];
    // get team list in org
    const teams = await octokit.rest.teams.list({
        org: organizationLogin,  
      });
    // add team_slug to projectsToAssociate
    for(const team_data of teams.data){
      const team_member = await octokit.rest.teams.listMembersInOrg({
            org: organizationLogin,
            team_slug: team_data.slug,
          });
      for(const assignee of assignees){
        if(team_member.data.find((m) => m.login === assignee.login)){
          if(projectMapping[team_data.slug]){
            projectsToAssociate.push(projectMapping[team_data.slug]);
            console.log("success get one team_slug");
          }
        }
      }
    }
    
    if (projectsToAssociate.length === 0) {
      console.log("Put it in the default project");
      projectsToAssociate.push(13); 
    }
    // deduplicate
    result = Array.from(new Set(projectsToAssociate))
    console.log(result);
    flag = true;
    if(result.includes(99999)){
      console.log("包含99999，flag设置为false");
      flag = false;
      result = result.filter(item => item !== 99999);
    }
    console.log("flag=",flag);
    const projectID_list = [];
    for(const projectId of result){
      var query = `
        query {
          organization(login: "${organizationLogin}") {
            projectV2(number: ${projectId}) {
              id
            }
          }
        }
      `;
      var options = {
          method: 'POST',
          headers: headers,
          body: JSON.stringify({ query }),
        };
      let pid;   // 存node-id
      const resp = await fetch(githubApiEndpoint, options);
      const resp_json = await resp.json();
      pid = resp_json.data.organization.projectV2.id;
      projectID_list.push(pid);
      console.log(pid);
    }
    let union_list = projectID_list.filter(v => issue_item_id.includes(v));
    console.log(union_list);
    // need delete project_item_id list
    let diff_del = issue_item_id.concat(union_list).filter(v => !issue_item_id.includes(v) || !union_list.includes(v));
    // need add project_item list
    let diff_add = projectID_list.concat(union_list).filter(v => !projectID_list.includes(v) || !union_list.includes(v));
    console.log("delete diff_del item");
    console.log(diff_del);
    console.log(diff_del.length !== 0 && flag);
    if(diff_del.length !== 0 && flag){
        for(const pid of diff_del){
        const del_item_id = m1.get(pid);
        var query=`
            mutation{
              deleteProjectV2Item(input:{projectId: \"${pid}\" itemId: \"${del_item_id}\" }){
                  deletedItemId  
                  }
            }
          `;
        var options = {
            method: 'POST',
            headers: headers,
            body: JSON.stringify({ query }),
          };
        await fetch(githubApiEndpoint, options);
        console.log("success delete item");
      }
    }
    console.log("add item");
    if(diff_add.length !== 0){
        for (const pid of diff_add) {
        var query=`
            mutation{
              addProjectV2ItemById(input:{projectId: \"${pid}\" contentId: \"${issue_node_id}\" }){
                  item  {
                     id   
                    }
                  }
            }
          `;
        var options = {
            method: 'POST',
            headers: headers,
            body: JSON.stringify({ query }),
          };
        // add item
        const resp_add = await fetch(githubApiEndpoint, options);
        await resp_add.json();
      }
    }
  } catch (error) {
    console.log(error.message)
  }
}

run();
