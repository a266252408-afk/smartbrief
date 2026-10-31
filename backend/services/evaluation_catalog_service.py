"""题库应用服务：HTTP管理读取与只读领域加载的边界。"""
from evaluation.catalog import CatalogError, VERSION, detail
from evaluation import catalog

__all__ = ['CatalogError', 'VERSION', 'summary', 'detail']


def summary(version=VERSION):
    from services.evaluation_review_service import listing
    from services.evaluation_service import EvaluationError
    data=catalog.summary(version)
    try:
        reviews=listing()
    except EvaluationError:
        raise CatalogError('REVIEW_STORAGE_FAILED','题目核验状态不可读，请检查审计记录') from None
    latest={}
    for row in reviews:
        if row['target']=='case_definition' and row['dataset_hash']==data['dataset_hash']:
            latest.setdefault(row['case_id'],row['decision'])
    for row in data['cases']:
        row['human_review']=latest.get(row['case_id'],row['human_review'])
    return data
