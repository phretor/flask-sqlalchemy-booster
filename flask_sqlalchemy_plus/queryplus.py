from flask_sqlalchemy import BaseQuery


class QueryPlus(BaseQuery):

    cls = None

    def desc(self, attr='id'):
        return self.order_by(getattr(self.cls, attr).desc())

    def asc(self, attr='id'):
        return self.order_by(getattr(self.cls, attr))
