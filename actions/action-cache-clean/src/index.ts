import * as github from "@actions/github";
import * as core from "@actions/core";



const token = core.getInput("github_token",{required: true});
let maxLiveDuration = parseInt(core.getInput("max_live_duration",{required: false}), 10);
let org = core.getInput("org",{required: false});

const octo = github.getOctokit(token);

function setDefaultValues() {
    maxLiveDuration = !isNaN(maxLiveDuration) ? maxLiveDuration * 1000 : 24* 60 * 60 * 1000;
    org = org != "" ? org : github.context.repo.owner;
}

async function deleteFroRepo(owner: string, repo: string) {
    core.info(`Start process repository ${org}/${repo}`)
    let page = 1;
    let per_page = 100;
    let needDeleteCaches = new Array<number>();
    while (true) {
        const {data: cacheList} =  await octo.rest.actions.getActionsCacheList(
            {
                owner: owner,
                repo: repo,
                page: page++,
                per_page: per_page,
            },
        );
        if (cacheList.total_count === 0) {
            core.info(`There is not action cache in repo ${owner}/${repo}, so skip it`);
            break;
        }
        const now = new Date();
        core.info(`Now is ${now.toISOString()}`);
        cacheList.actions_caches.forEach(cache => {
            if (cache.key === undefined) {
                core.warning("The Key of cache is undefined, so skip it");
                return;
            }
            if (cache.id === undefined) {
                core.warning("The ID of cache is undefined, so skip it");
                return;
            }
            core.info(`Start process cache ${cache.key} for repository ${owner}/${repo}`);

            if (cache.last_accessed_at === undefined) {
                core.info(`The last access time of cache ${cache.key} is undefined, so delete it`);
                needDeleteCaches.push(cache.id)
                return;
            }

            const lastAccessTime = Date.parse(cache.last_accessed_at);
            const result = now.getTime() - lastAccessTime > maxLiveDuration;

            core.debug(`now.getTime() - lastAccessTime = ${now.getTime() - lastAccessTime}`)
            core.debug(`maxLiveDuration = ${maxLiveDuration}`)

            core.info(`The result of 'now.getTime() - lastAccessTime > maxLiveDuration' is ${result}`);
            if (result) {
                needDeleteCaches.push(cache.id)
            }
        })
        if (cacheList.total_count < per_page) {
            break;
        }
    }

    needDeleteCaches.forEach(id => {
        deleteCache(owner, repo, id);
    })
    return null;
}

function deleteCache(owner: string, repo: string,id: number) {
    octo.rest.actions.deleteActionsCacheById(
        {
            owner: owner,
            repo: repo,
            cache_id:id,
        },
    ).then(
        (data) => {
            core.info(`Delete cache in ${owner}/${repo} with id ${id} finished,
             response status: ${data.status}`);
        }
    )
}

async function run() {
    let page = 1;
    let per_page = 100;
    while (true) {
        const {data: repos} = await octo.rest.repos.listForOrg(
            {
                org: org,
                page: page++,
                per_page: per_page,
            },
        )
        repos.forEach((repo) => {
            deleteFroRepo(org,repo.name)
        })
        if (repos.length < per_page) {
            break
        }
    }
    return null
}

async function main() {
    setDefaultValues()
    try {
        await run();
    }catch (err) {
        core.setFailed(err as Error);
    }
}

main();