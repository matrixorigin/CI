import copy
from pathlib import Path
import unittest

import node_cache_real_pr_gate as gate
import build_real_pr_node_cache_workflow as workflow


class CurrentPR(unittest.TestCase):
    def fixture(self):
        context={'repository':'matrixorigin/matrixone','event':'workflow_dispatch','ref':'refs/heads/weilu-runner-test',
            'actor':'Ariznawlll','attempt':'1','pr_number':32901,'expected_sha':'a'*40}
        pr={'number':32901,'state':'open','draft':False,'base':{'ref':'main','repo':{'full_name':'matrixorigin/matrixone'}},
            'head':{'sha':'a'*40,'repo':{'full_name':'trusted-user/matrixone','archived':False}},
            'user':{'login':'trusted-user'},'labels':[{'name':'kind/test-ci'}]}
        return context,pr

    def test_current_trusted_fork_snapshot_outputs_exact_actual_identity(self):
        context,pr=self.fixture();before=copy.deepcopy((context,pr));value=gate.authorize(context,pr,204)
        self.assertEqual((context,pr),before);self.assertEqual(value['head_repository'],'trusted-user/matrixone')
        self.assertEqual(value['head_sha'],'a'*40);self.assertEqual(value['allowed'],'true')
        self.assertEqual(value['project_cache_namespace'],'matrixorigin/matrixone')

    def test_closed_draft_moved_fork_untrusted_author_unsafe_label_or_other_caller_refused(self):
        for fault in ('closed','draft','sha','repo','label','member','actor','branch','retry','base','number'):
            context,pr=self.fixture();membership=204
            if fault=='closed':pr['state']='closed'
            if fault=='draft':pr['draft']=True
            if fault=='sha':pr['head']['sha']='b'*40
            if fault=='repo':pr['head']['repo']['full_name']='other;command'
            if fault=='label':pr['labels']=[]
            if fault=='member':membership=404
            if fault=='actor':context['actor']='other'
            if fault=='branch':context['ref']='refs/heads/main'
            if fault=='retry':context['attempt']='2'
            if fault=='base':pr['base']['repo']['full_name']='other/project'
            if fault=='number':context['pr_number']=1
            with self.subTest(fault=fault),self.assertRaises(ValueError):gate.authorize(context,pr,membership)

    def test_validation_inherits_full_two_group_consumer_without_precompile_or_fake_pr_event(self):
        w=workflow.build();job=w['jobs']['ut-coverage-linux-x86']
        self.assertEqual(set(w['on']['workflow_call']['inputs']),{'pr_number','expected_head_sha'})
        self.assertEqual(job['runs-on'],"${{ matrix.group == 0 && 'cs-mty-nc-n0-pr-v1' || 'cs-mty-nc-n1-pr-v1' }}")
        self.assertEqual(job['strategy'],{'fail-fast':False,'max-parallel':2,'matrix':{'group':[0,1]}})
        self.assertNotIn('concurrency',w);self.assertNotIn('concurrency',job)
        source=next(s for s in job['steps'] if s.get('name')=='Checkout MatrixOne source')
        self.assertEqual(source['with']['ref'],'${{ needs.check_coverage_eligibility.outputs.head_sha }}')
        self.assertEqual(source['with']['repository'],'${{ needs.check_coverage_eligibility.outputs.head_repository }}')
        rendered=workflow.render()
        self.assertNotIn('github.event.pull_request',rendered)
        commands='\n'.join(s.get('run','') for s in job['steps'])
        self.assertTrue('node_cache_candidate.py' not in commands)
        self.assertFalse(any('download-artifact@' in s.get('uses','') for s in job['steps']))
        self.assertIn('check_coverage_eligibility',w['jobs']['merge-coverage-groups']['needs'])
        self.assertIn('node_cache_real_pr_gate.py',rendered)
        self.assertEqual((workflow.consumer.ROOT/workflow.TARGET).read_text(),rendered)


if __name__=='__main__':unittest.main()
