package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"issue-no_pull-request-action/pkg/config"
	ihttp "issue-no_pull-request-action/pkg/http"
	"net/http"
)

func main() {
	// 设置GitHub API的基本URL和认证信息
	//Examples of parameters are as follows
	//baseURL := "https://api.github.com"
	//owner := "Rosyrain"
	//repo := "github_action_test"
	//issueNumber := 1
	//token := "xxxxxxx"
	//assignees := "Rosyrain"
	//labelData := `["needs-review","no-pull_request"]`

	baseURL := config.GetBaseURL()
	owner := config.GetOwner()
	repo := config.GetRepository()
	issueNumber := config.GetIssueNumber() // 你要检查的issue编号
	token := config.GetGithubToken()       // 从环境变量中获取GitHub访问令牌
	assignees := config.GetAssignees()
	labelData := config.GetLabels()

	fmt.Println("owner:", owner)
	hasRelatedPR := false // 是否存在关联pr

	// 构建请求URL
	issueURL := fmt.Sprintf("%s/repos/%s/issues/%d/timeline", baseURL, repo, issueNumber)

	// 创建请求头，包含认证信息
	issueResp, err := ihttp.Request("GET", issueURL, token, nil, "")
	if err != nil {
		panic(fmt.Sprintf("issueUrl http.Request failed,err:%v", err))
	}
	if issueResp.StatusCode != http.StatusOK {
		panic(fmt.Sprintf("connect issueUrl failed,resp.statusCode:%d", issueResp.StatusCode))
	}
	defer issueResp.Body.Close()
	fmt.Printf("get issue %d info successfully.\n", issueNumber)

	// 解析响应内容（这里省略了具体的解析过程，你可以根据需要自行处理）
	// 检查issue是否有关联的pull request
	body, err := io.ReadAll(issueResp.Body)
	if err != nil {
		panic(fmt.Sprintf("Error reading issue response body:%v", err))
	}
	var issueData []interface{}
	err = json.Unmarshal(body, &issueData)
	if err != nil {
		panic(fmt.Sprintf("Error unmarshalling JSON:%v", err))
	}

	for _, data := range issueData {
		dataMap := data.(map[string]interface{})
		typeEvent := dataMap["event"]
		if typeEvent == "cross-referenced" {
			sourceMap := dataMap["source"].(map[string]interface{})
			issueMap := sourceMap["issue"].(map[string]interface{})
			_, ok := issueMap["pull_request"]
			if ok {
				hasRelatedPR = true
				break
			}
		}
	}

	//如果不存在关联pr
	if !hasRelatedPR {
		// 如果没有关联的pull request，给issue添加标签并转交给sukki37
		// 添加标签
		labelURL := fmt.Sprintf("%s/repos/%s/issues/%d/labels", baseURL, repo, issueNumber)
		labelResp, err := ihttp.Request("POST", labelURL, token, bytes.NewReader([]byte(labelData)), "application/json")
		if err != nil {
			panic(fmt.Sprintf("Error adding label,err:%v", err))
		}
		if labelResp.StatusCode != http.StatusOK {
			panic(fmt.Sprintf("labelURL http.Request failed,labelStatusCode:%d", labelResp.StatusCode))
		}
		defer labelResp.Body.Close()
		fmt.Printf("Add labels %s successfully.\n", labelData)

		// 转交给sukki37
		assigneeURL := fmt.Sprintf("%s/repos/%s/issues/%d/assignees", baseURL, repo, issueNumber)
		assigneeData := map[string]string{"assignees": assignees}
		jsonData, err := json.Marshal(assigneeData)

		assigneeResp, err := ihttp.Request("POST", assigneeURL, token, bytes.NewReader(jsonData), "application/json")
		if err != nil {
			panic(fmt.Sprintf("Error creating assignee request:%v", err))
		}
		if assigneeResp.StatusCode != 201 {
			panic(fmt.Sprintf("Failed to assign a problem to a specified person,assigneeStatusCode:%d", assigneeResp.StatusCode))
		}
		defer assigneeResp.Body.Close()
		fmt.Printf("Issue labeled and assigned to %s successfully.\n", assignees)

		//重新打开issue
		reopenURL := fmt.Sprintf("%s/repos/%s/issues/%d", baseURL, repo, issueNumber)
		reopenData := map[string]string{"state": "open"}
		jsonData, err = json.Marshal(reopenData)
		if err != nil {
			panic(fmt.Sprintf("Error marshaling JSON:%v", err))
		}

		reopenResp, err := ihttp.Request("PATCH", reopenURL, token, bytes.NewReader(jsonData), "application/json")
		if err != nil {
			panic(fmt.Sprintf("Error creating reopen request:%v", err))
		}
		if reopenResp.StatusCode != http.StatusOK {
			panic(fmt.Sprintf("Failed to reopen topic,reopenStatusCode:%d", reopenResp.StatusCode))
		}
		defer reopenResp.Body.Close()
		fmt.Printf("Issue %d reopen successfully.\n", issueNumber)
	} else {
		fmt.Println("Issue has related pull requests, no action taken.")
	}
}
