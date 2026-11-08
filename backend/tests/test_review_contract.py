"""审核契约真实基线及数字识别回归。"""
import pytest
from services.citations import quantitative_tokens
from services.review_contracts import ReviewResult

def test_section_references_are_not_quantities():
    assert quantitative_tokens('参见第一章1.1节与1.2节。') == []
    assert quantitative_tokens('见第2.3节，增长1.2%。') == ['1.2%']

def test_review_contract_rejects_numeric_id():
    with pytest.raises(ValueError):
        ReviewResult.model_validate({'passed':False,'comments':['修改'],'findings':[{'finding_id':1}]})
